# voidx 冷启动优化方案

> **Status: Done**
> 日期: 2026-06-07

## 目标

减少 voidx 冷启动（新 session，不 resume）到用户可输入的耗时。

本期目标是让输入循环更早启动，不改变 session 选择、profile 选择、MCP 连接、LSP 工具语义。后台化只允许发生在启动提示之后、用户输入循环之前不再强依赖的工作上。

## 当前启动路径

```
main() → _run_chat()
  ├─ set_dock(BottomInputDock())
  ├─ Settings.create(ws_path)              ← JSON 文件 I/O + legacy profile SQLite 迁移
  │   └─ _migrate_legacy_profiles()        ← async, 仅首次有实际工作
  ├─ bind_settings(settings)
  ├─ settings.resolve_profile()            ← async, SQLite 读取
  ├─ settings.build_config(profile=...)    ← async, 复用已解析 profile
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
      │   └─ LspManager()                  ← 纯内存
      └─ graph.run()
          ├─ _restore_runtime_state()      ← async, 新 session 时跳过
          ├─ _show_startup()               ← UI 渲染
          ├─ PureTui() 构建                ← 纯内存
          ├─ LSP initialize/doctor task     ← 后台 to_thread + 生命周期清理
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

**方案**:
1. `Settings.build_config()` 增加可选 `profile` 参数。未传时保持现有行为，传入时直接复用，不再查询 SQLite。
2. `_run_chat()` 先 `profile = await settings.resolve_profile()`，再 `cfg = await settings.build_config(profile=profile)`。
3. 修复 `_run_chat()` 中 `settings.resolve_api_key(...)` 缺少 `await` 的问题。
4. CLI `--provider` 覆盖后，如果当前 profile provider 不匹配，应回退到 `await settings.resolve_api_key(cfg.model.provider)`，避免误用旧 provider 的 key。

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

**方案**: 将 `load_lsp_servers()` 从构造函数移出，在 `run()` 中后台执行。LSP 客户端本身就是按需启动的（`_ensure_client`），启动时不需要完整的 server 列表。

具体做法：
1. `LspManager.__init__` 只做最小初始化，不调 `load_lsp_servers()`
2. 新增 `async def initialize(self)` 方法，通过 `asyncio.to_thread(load_lsp_servers, workspace)` 执行重 I/O，避免阻塞 event loop
3. 在 `run()` 中 `asyncio.create_task(self._lsp_manager.initialize())`
4. `doctor()` / `statuses()` 这类同步展示接口在未就绪时返回 initializing 状态，不触发 I/O
5. `open_document()` / `_ensure_client()` 这类工具路径必须 `await initialize()`，保证用户刚启动就调用 LSP 工具时不会误报“无 server 配置”
6. 初始化完成后一次性替换 `_servers`，避免外部遍历时看到半更新状态
7. `restart()` 全量重启时也通过 `to_thread(load_lsp_servers, workspace)` 重新加载 server 列表

**预期收益**: 消除启动路径中最重的同步 I/O，预计节省 50-200ms（取决于系统上安装的 IDE/包数量）。

---

### 3. LSP 可用性过滤改为缓存状态

**问题**: `filter_unavailable_lsp_tools()` 每次 LLM 调用前通过 `lsp_manager.doctor()` 判断是否暴露 LSP 工具。`doctor()` 会解析命令可用性，优化后也不应在工具过滤路径触发 I/O。

**位置**:
- `src/voidx/agent/tool_filters.py:18` — `_has_available_lsp_server()` 调 `doctor()`
- `src/voidx/agent/graph/core.py:556` — 每次 `_call_llm()` 前过滤工具

**方案**:
1. `LspManager` 暴露 `has_available_server()`，只读取已缓存的 `_servers` 状态。
2. 初始化未完成时返回 `False`，避免启动早期把未确认可用的 LSP 工具暴露给模型。
3. 初始化完成后，如果任一 enabled server 有 `resolved_command` 或 command 当前可解析，则返回 `True`。命令解析结果只在初始化/doctor 背景任务中更新缓存，不在过滤路径做扫描。

**预期收益**: 避免 LLM 前置路径意外执行 LSP doctor 检查。

---

### 4. LSP doctor 显示延迟

**问题**: `run()` 中在进入输入循环前同步遍历 `doctor()` 结果并显示。`doctor()` 内部对每个语言服务器调用 `shutil.which()`。

**位置**: `src/voidx/agent/graph/run_loop.py:197-204`

**方案**:
1. 在 `PureTui` 构建完成后启动后台 task。
2. 后台 task 先等待 `LspManager.initialize()` 完成，再调用 `doctor()` 读取缓存/轻量检查并追加可用 server 提示。
3. `run()` 的 `finally` 必须 cancel/await 该 task，避免 TUI/dock 关闭后还有后台输出。

**预期收益**: 小幅优化（~5-20ms），但让输入循环更早可用。

---

### 5. `_migrate_legacy_profiles()` 暂不后台化

**问题**: `Settings.create()` 中 `await self._migrate_legacy_profiles()` 阻塞启动。大多数情况下是空操作（无旧配置），但首次迁移时涉及 SQLite 写入。

**位置**: `src/voidx/config/settings.py:52`

**决策**: 本期不后台化。

原因：
1. 首次启动时 profile 解析可能依赖 legacy profile 迁移结果。
2. 如果 `resolve_profile()` 必须等待迁移完成，后台化不会改善关键路径。
3. 引入 migration event 会增加竞态和测试成本，收益只覆盖首次迁移场景。

后续可选优化：在 `_migrate_legacy_profiles()` 开头增加 fast-path，当 settings JSON 不包含 legacy keys 时立即返回。

---

## 优先级排序

| 优先级 | 优化项 | 预期收益 | 实现复杂度 |
|--------|--------|---------|-----------|
| P0 | 消除 `resolve_profile()` 重复调用 | 中 | 低 |
| P0 | LSP `detect_servers()` 延迟到后台 | 高 | 中 |
| P0 | LSP 工具按需等待初始化 | 正确性 | 中 |
| P1 | LSP 可用性过滤改缓存状态 | 中 | 低 |
| P1 | LSP doctor 显示延迟 | 低 | 低 |
| P2 | `_migrate_legacy_profiles()` fast-path | 低（仅首次） | 低 |
| 不做 | `_migrate_legacy_profiles()` 后台化 | 低（仅首次） | 中 |

## 测试计划

| 测试 | 覆盖点 |
|------|--------|
| `test_build_config_uses_pre_resolved_profile_once` | `_run_chat()` / `build_config(profile=...)` 不重复查询 profile |
| `test_run_chat_awaits_resolve_api_key_when_no_profile` | 无 profile 时正确 await `resolve_api_key()` |
| `test_run_chat_uses_provider_specific_key_after_cli_override` | CLI provider 覆盖后不误用旧 profile key |
| `test_lsp_manager_constructor_does_not_load_servers` | 构造 LSP manager 不触发 `load_lsp_servers()` |
| `test_lsp_initialize_runs_load_in_thread` | 初始化通过后台线程加载 server 列表 |
| `test_lsp_tool_waits_for_initialization` | 刚启动立刻调用 LSP 工具会等待初始化，而不是误报无配置 |
| `test_lsp_doctor_reports_initializing_without_io` | 未初始化时 doctor/status 不触发 I/O |
| `test_tool_filter_uses_cached_lsp_availability` | LSP 工具过滤不调用 doctor |
| `test_run_loop_cancels_lsp_startup_tasks_on_exit` | run loop 退出时清理后台 LSP task |

## 不需要优化的部分

以下步骤在冷启动中已经是空操作或极快，无需优化：

- **`_restore_runtime_state()`** — 新 session 时 `self._session is None`，直接返回
- **`_restore_transcript_snapshot()`** — 新 session 时同上
- **`_select_start_session()`** — 不 resume 时直接返回 None
- **MCP `start_all()`** — 已经是 fire-and-forget
- **`InstructionService.__init__`** — 纯内存，无 I/O
- **`build_graph()`** — 纯内存编译，<1ms
- **`build_tool_registry()`** — 纯内存
