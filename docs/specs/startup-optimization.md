# voidx 冷启动优化方案

> **Status: Draft**
> 日期: 2026-06-07

## 目标

减少 voidx 冷启动（新 session，不 resume）到用户可输入的耗时。

## 当前启动路径

```
main() → _run_chat()
  ├─ set_dock(BottomInputDock())
  ├─ Settings.create(ws_path)              ← async, SQLite + 文件 I/O
  │   └─ _migrate_legacy_profiles()        ← async, 仅首次有实际工作
  ├─ bind_settings(settings)
  ├─ settings.build_config()               ← async, 内部调 resolve_profile()
  ├─ settings.resolve_profile()            ← async, SQLite 读取（重复！）
  ├─ _select_start_session()               ← async, 新 session 时直接返回 None
  └─ VoidXGraph(cfg, api_key, ...)
      ├─ create_chat_model()               ← 纯内存
      ├─ build_tool_registry()             ← 纯内存
      ├─ InstructionService()              ← 纯内存
      ├─ build_permission_service()        ← 纯内存
      ├─ build_compaction_service()        ← 纯内存
      ├─ _build() (LangGraph 编译)         ← 纯内存
      ├─ SlashHandler()                    ← 纯内存
      ├─ build_external_managers()
      │   ├─ McpManager()                  ← 纯内存
      │   └─ LspManager()
      │       └─ load_lsp_servers()        ← ⚠️ 重 I/O: detect_servers()
      └─ graph.run()
          ├─ _restore_runtime_state()      ← async, 新 session 时跳过
          ├─ _show_startup()               ← UI 渲染
          ├─ PureTui() 构建                ← 纯内存
          ├─ LSP doctor() 显示             ← shutil.which × N
          ├─ MCP start_all()               ← 已是后台 (create_task)
          └─ app.run() / app.run_headless()← 进入输入循环
```

## 优化项

### 1. 消除 `resolve_profile()` 重复调用

**问题**: `_run_chat()` 中先调 `settings.resolve_profile()` 拿 api_key，而 `build_config()` 内部又调了一次 `resolve_profile()`，两次都走 SQLite 查询。

**位置**:
- `src/voidx/main.py:73` — `cfg = await settings.build_config()`
- `src/voidx/main.py:81` — `profile = await settings.resolve_profile()`
- `src/voidx/config/settings.py:193` — `build_config()` 内部 `profile = await self.resolve_profile()`

**方案**: `build_config()` 返回 profile 信息，或 `_run_chat()` 先调 `resolve_profile()` 再把结果传给 `build_config()`，避免重复查询。

**预期收益**: 省一次 SQLite 查询 + 一次 `list_model_profiles_async()` 调用。

---

### 2. LSP `detect_servers()` 延迟到后台

**问题**: `LspManager.__init__` 中同步调用 `load_lsp_servers()`，内部 `detect_servers()` 会扫描：
- IDE 扩展目录（VS Code, Cursor, Windsurf 等）
- npm 全局包
- pip 包
- Neovim Mason
- 系统路径（Homebrew, Xcode CLT 等）
- PATH (`shutil.which`)

这是整个启动路径中最重的 I/O 操作，涉及大量文件系统遍历。

**位置**:
- `src/voidx/lsp/manager.py:30` — `self._servers = load_lsp_servers(self.workspace)`
- `src/voidx/lsp/config.py:72-87` — `load_lsp_servers()` → `_apply_auto_detection()` → `detect_servers()`
- `src/voidx/lsp/detector.py:34-54` — `detect_servers()` 6 个探测器串行执行

**方案**: 将 `load_lsp_servers()` 改为异步，在 `run()` 中后台执行。LSP 客户端本身就是按需启动的（`_ensure_client`），启动时不需要完整的 server 列表。

具体做法：
1. `LspManager.__init__` 只做最小初始化，不调 `load_lsp_servers()`
2. 新增 `async def initialize(self)` 方法，在后台执行 `load_lsp_servers()`
3. 在 `run()` 中 `asyncio.create_task(self._lsp_manager.initialize())`
4. `doctor()` / `statuses()` / `_ensure_client()` 等方法在 `_servers` 未就绪时返回空或等待

**预期收益**: 消除启动路径中最重的同步 I/O，预计节省 50-200ms（取决于系统上安装的 IDE/包数量）。

---

### 3. LSP doctor 显示延迟

**问题**: `run()` 中在进入输入循环前同步遍历 `doctor()` 结果并显示。`doctor()` 内部对每个语言服务器调用 `shutil.which()`。

**位置**: `src/voidx/agent/graph/run_loop.py:197-204`

**方案**: 将 LSP doctor 显示移到输入循环启动后，通过 `asyncio.create_task` 在后台执行，结果通过 `dock.append_message()` 异步追加。

**预期收益**: 小幅优化（~5-20ms），但让输入循环更早可用。

---

### 4. `_migrate_legacy_profiles()` 后台化

**问题**: `Settings.create()` 中 `await self._migrate_legacy_profiles()` 阻塞启动。大多数情况下是空操作（无旧配置），但首次迁移时涉及 SQLite 写入。

**位置**: `src/voidx/config/settings.py:52`

**方案**: 迁移逻辑不影响后续启动流程（profile 数据已在新表中），可以 `asyncio.create_task` 后台执行。需要确保与后续 profile 操作的竞态安全——如果迁移尚未完成时有 profile 查询，应等待迁移完成。

简单做法：加一个 `_migration_done: asyncio.Event`，`resolve_profile()` 等待该 event。

**预期收益**: 仅首次使用时有意义，日常启动无影响。优先级低。

---

## 优先级排序

| 优先级 | 优化项 | 预期收益 | 实现复杂度 |
|--------|--------|---------|-----------|
| P0 | 消除 `resolve_profile()` 重复调用 | 中 | 低 |
| P0 | LSP `detect_servers()` 延迟到后台 | 高 | 中 |
| P1 | LSP doctor 显示延迟 | 低 | 低 |
| P2 | `_migrate_legacy_profiles()` 后台化 | 低（仅首次） | 中 |

## 不需要优化的部分

以下步骤在冷启动中已经是空操作或极快，无需优化：

- **`_restore_runtime_state()`** — 新 session 时 `self._session is None`，直接返回
- **`_restore_transcript_snapshot()`** — 新 session 时同上
- **`_select_start_session()`** — 不 resume 时直接返回 None
- **MCP `start_all()`** — 已经是 fire-and-forget
- **`InstructionService.__init__`** — 纯内存，无 I/O
- **`build_graph()`** — 纯内存编译，<1ms
- **`build_tool_registry()`** — 纯内存
