# LSP 启动预热与超时优化 — 技术设计文档

> **Status: Done** — 已实现启动预热、30s LSP 请求超时、启动 UI warmup 状态展示和测试覆盖。

## Context

voidx 的 LSP 工具（`lsp_diagnostics`、`lsp_symbols`、`lsp_definition`、`lsp_references`、`lsp_format`）在首次调用时几乎必然超时。根本原因有两个：

1. **LSP 服务器进程是懒启动的** — 启动时只做了配置检测（`load_lsp_servers` 扫描 PATH 和 IDE 扩展），不启动进程、不建索引。首次调用工具时才触发 `_ensure_client()` → `client.start()`，冷启动 + 项目索引全挤在一次请求里。
2. **默认超时 10 秒太短** — `LspClient.request()` 和 `LspClient.start()` 的默认 timeout 都是 10 秒。对 pyright、rust-analyzer 等需要建项目索引的 LSP 服务器，10 秒远远不够。

用户在启动时看到 LSP 检测结果（如 `python → pyright-langserver`），误以为 LSP 已就绪，实际只是知道命令路径。

## Goals and Non-Goals

### Goals

- 启动时预热 LSP 服务器进程，让首次工具调用无需等待冷启动
- 提高请求超时上限，覆盖大项目索引场景
- 预热失败不阻塞启动，不影响正常使用
- 启动 UI 展示预热状态（连接中 / 已就绪 / 失败）

### Non-Goals

- 不改变 LSP 配置加载逻辑（`load_lsp_servers` 保持不变）
- 不改变 LSP JSON-RPC 协议交互方式
- 不做 LSP 服务器健康检查轮询（预热只在启动时做一次）

## Architecture

### 当前流程

```
启动 → show_lsp_startup()
         ├── manager.initialize()          # 只加载配置，不启动进程
         └── manager.doctor()              # 检查命令是否在 PATH
         → 显示 "python → pyright-langserver"

首次工具调用 → open_document() → _ensure_client()
                                ├── client.start()              # 冷启动进程
                                │   └── request("initialize")   # 10s 超时
                                └── client.request("textDocument/didOpen")
                                    └── request(...)             # 10s 超时
```

### 改后流程

```
启动 → show_lsp_startup()
         ├── manager.initialize()          # 加载配置
         ├── manager.warm_up()             # 新增：预热所有可用服务器
         │   └── 对每个 enabled 且命令可解析的服务器:
         │       └── _ensure_client(language, timeout=30.0)
         │           └── client.start(timeout=30.0)  # 启动进程 + initialize 握手
         └── 显示预热结果
              "python → pyright-langserver (warming…)"
              "python → pyright-langserver ✓ ready"  或  "python → pyright-langserver failed: <reason>"

首次工具调用 → open_document() → _ensure_client()
                                └── client 已连接，直接复用    # 无冷启动
```

## Data Model

无新增数据模型。`LspManager` 内部状态已有 `_clients`、`_errors` 字段，预热结果自然存入其中。

`LspRuntimeStatus` 已有 `status` 字段（`connected` / `disconnected` / `error`），预热后状态自动更新。

## API Contract

### `LspManager.warm_up()`

- **Signature**: `async def warm_up(self, *, timeout: float = 30.0) -> dict[str, str]`
- **Behavior**: 对所有 `enabled` 且命令可解析的服务器并发预热；不可解析的服务器跳过，不写入 error。可预热服务器调用 `_ensure_client(language, timeout=timeout)`。返回 `{language: status}` 映射，status 为 `"ok"` / `"error: <message>"`。
- **Errors**: 单个服务器预热失败不影响其他服务器。失败信息记入 `self._errors`。
- **Timeout**: 每个服务器的 `client.start()` 传入 `timeout` 参数（默认 30s）。

### `LspManager._ensure_client()`

- **Before**: `async def _ensure_client(self, language: str) -> LspClient`
- **After**: `async def _ensure_client(self, language: str, *, timeout: float = 30.0) -> LspClient`
- **Behavior**: 已连接时直接复用；未连接时用传入 timeout 调用 `client.start()`。

### `LspClient.start()` timeout 参数

- **Before**: `timeout: float = 10.0`
- **After**: `timeout: float = 30.0`

### `LspClient.request()` timeout 参数

- **Before**: `timeout: float = 10.0`
- **After**: `timeout: float = 30.0`

### `LspManager` 调用 `client.request()` 的地方

以下调用点显式传 `timeout=30.0`，不依赖默认值：

| 方法 | 请求 | 当前 | 改后 |
|------|------|------|------|
| `document_symbols` | `textDocument/documentSymbol` | 默认 10s | 30s |
| `workspace_symbols` | `workspace/symbol` | 默认 10s | 30s |
| `definition` | `textDocument/definition` | 默认 10s | 30s |
| `references` | `textDocument/references` | 默认 10s | 30s |
| `format_document` | `textDocument/formatting` | 默认 10s | 30s |

### 启动 UI

`show_lsp_startup()` 仍在后台 task 中运行，不阻塞 REPL。展示分两步：

1. `manager.initialize()` 后先打印可用服务器列表，状态为 `warming...`，让用户知道已检测到但仍在预热。
2. `manager.warm_up()` 返回后打印结果：成功显示 `ready`，失败显示 `failed: <message>`。

如果 `warm_up()` 不存在（测试或旧 adapter fake manager），run loop 保持只展示 doctor 检测结果，避免破坏轻量 mock。

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| 预热时某个 LSP 服务器启动超时 | 记入 `self._errors`，返回 `"error: LSP request timed out"`，不影响其他服务器 |
| 预热时 LSP 服务器进程崩溃 | `_ensure_client()` 抛 `LspConnectionError`，捕获后记入 `self._errors` |
| 预热前命令不可解析 | 跳过该服务器，不写入 `self._errors`，保持 `disconnected` 状态 |
| 预热时命令在解析后消失 | `_ensure_client()` 抛 `LspServerUnavailable`，捕获后记入 `self._errors` |
| 预热整体被取消（用户退出） | `show_lsp_startup()` 已有 `asyncio.CancelledError` 处理，预热任务随主循环一起取消 |
| 预热后服务器断连 | 下次工具调用时 `_ensure_client()` 检测到 `not client.connected`，重新启动 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 启动时预热所有可用服务器 | 按需启动（现状） | 预热消除首次调用延迟，代价只是启动时多等几秒 |
| 并发预热 | 串行预热 | 并发更快，LSP 服务器之间无依赖 |
| 默认超时从 10s 提到 30s | 提到 60s 或不设超时 | 30s 覆盖绝大多数项目；60s 过于保守；不设超时风险太大 |
| 预热失败不阻塞启动 | 预热失败时阻止使用 LSP 工具 | LSP 是辅助功能，不应因预热失败影响核心流程 |
| `warm_up()` 返回状态映射 | 无返回值，靠 `statuses()` 查询 | 启动 UI 需要直接拿到结果来展示，`statuses()` 需要额外调用 |

## Test Plan

| 测试 | 覆盖 |
|------|------|
| `test_lsp_manager_warm_up_starts_available_servers` | `warm_up()` 预热 fake LSP，并让 status 变为 `connected` |
| `test_lsp_manager_warm_up_skips_unavailable_servers` | 命令不可解析的服务器不预热、不写 error |
| `test_lsp_manager_warm_up_records_per_server_errors` | 单个语言预热失败写入 `_errors`，不影响其它语言 |
| `test_lsp_manager_requests_use_extended_timeout` | manager 的 symbol/definition/references/format 调用显式传 `timeout=30.0` |
| `test_run_loop_lsp_startup_warms_servers` | 启动后台 task 调用 `manager.warm_up()` 并显示 ready/failed |
| 现有 `test_run_loop_cancels_lsp_startup_tasks_on_exit` | 启动 task 取消路径仍然可取消 |

## Open Questions

- [ ] 预热超时是否应可配置（如 `.voidx/lsp.json` 里的 `warmup_timeout` 字段）？当前先用硬编码 30s，后续按需加。
- [ ] 是否需要在 TUI 状态栏显示 LSP 连接状态？当前只在启动时打印一行，后续可加。
