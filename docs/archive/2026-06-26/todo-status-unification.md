> **Status: Done**

# Todo 状态值统一

## Goal

将 todo 状态值从 `pending | in_progress | completed | cancelled` 统一为 `pending | active | done`，移除 `cancelled`（取消的项直接删除）。

## Architecture

状态值是核心类型 `TodoStatus`，被 `TodoRunItem`、`TodoItem`、`TodoUpdateOp`、UI 渲染、持久化等引用。
改动从 `TodoStatus` 定义出发，逐层更新所有引用方。`cancelled` 不再作为状态值——update 操作收到 `cancelled` 时直接从 tracker 删除该项。

## 状态映射

| 旧状态 | 新状态 | 说明 |
|--------|--------|------|
| `pending` | `pending` | 不变 |
| `in_progress` | `active` | 正在做 |
| `completed` | `done` | 做完了 |
| `cancelled` | （删除） | update 时直接移除该项 |

## File Structure & Tasks

### Task 1: 核心类型定义

- [ ] `src/voidx/runtime/todo.py:7` — `TodoStatus` 改为 `Literal["pending", "active", "done"]`
- [ ] `src/voidx/runtime/task_state.py:50` — `TodoRunItem.status` 改为 `Literal["pending", "active", "done"]`
- [ ] `src/voidx/runtime/task_state.py:53-62` — `TodoRunState`: `in_progress` 字段改名 `active`，移除 `cancelled` 字段，`done` 字段含义变为 `count(status=="done")`
- 测试: `.venv\Scripts\python.exe -m pytest tests/test_agent/test_todo_events.py -v`

### Task 2: todo 工具

- [ ] `src/voidx/tools/todo.py:22` — `TodoItem.status` description 改为 `pending | active | done`
- [ ] `src/voidx/tools/todo.py:34` — `TodoReadFilter` 改为 `Literal["all", "pending", "active", "done"]`
- [ ] `src/voidx/tools/todo.py:58-65` — `TodoWriteTool.description` 状态说明改为 `pending → active → done`
- [ ] `src/voidx/tools/todo.py:109-112,117,132-133` — `_execute_read` 计数和 ICONS 改为新状态值
- [ ] `src/voidx/tools/todo.py:170-177` — `_execute_update`: update 设为 `cancelled` 时直接删除该项（从 `current_todos` 移除）
- [ ] `src/voidx/tools/todo.py:184-187,192,203-208,214` — `_execute_update` 计数和 ICONS 改为新状态值
- [ ] `src/voidx/tools/todo.py:260-263,268,272,278,282-284` — `_execute_write` 计数和 ICONS 改为新状态值
- [ ] `src/voidx/tools/todo.py:289-297` — `_filter_items` 简化：移除 `done` 特殊分支，直接按状态值过滤
- 测试: `.venv\Scripts\python.exe -m pytest tests/test_tools/test_todo.py -v` (如存在)

### Task 3: todo_state 转换层

- [ ] `src/voidx/agent/todo_state.py:48-54` — `todo_run_state_from_result`: 计数改用新状态值，`active_items` 筛选改为 `status=="active"`
- [ ] `src/voidx/agent/todo_state.py:60-62` — `TodoRunState` 构造: `active=active`, 移除 `cancelled=cancelled`
- 测试: `.venv\Scripts\python.exe -m pytest tests/test_agent/test_todo_events.py -v`

### Task 4: runtime_context 渲染

- [ ] `src/voidx/agent/runtime_context.py:276-280` — `_current_task_state`: 用 `todo_state.items` 筛选 `active`+`pending`，只输出 id 列表，末尾加 `Call todo with op=read for details.`
- 测试: `.venv\Scripts\python.exe -m pytest tests/test_agent/test_runtime_context_builder.py -v`

### Task 5: runtime_guards

- [ ] `src/voidx/agent/graph/runtime_guards.py:353-372` — `todo_status_signature`: `(done, active, pending)` 三元组，移除 `cancelled`
- 测试: `.venv\Scripts\python.exe -m pytest tests/test_agent/test_runtime_guards.py -v`

### Task 6: UI 渲染

- [ ] `src/voidx/ui/output/dock/todo.py:13-18` — `TODO_STATUS_ORDER` 改为 `("active", "pending", "done")`，`TODO_ICONS` 改键名
- [ ] `src/voidx/ui/tui/render_todo.py:12-23` — `_TODO_PINNED_ORDER` 改为 `("active", "pending", "done")`，`_TODO_PINNED_ICONS` 和 `_TODO_PINNED_STYLES` 改键名
- 测试: `.venv\Scripts\python.exe -m pytest tests/ -k "todo or dock or render" -q`

### Task 7: 测试更新

- [ ] `tests/test_agent/test_todo_events.py` — 所有 `in_progress`→`active`，`completed`→`done`，移除 `cancelled` 字段
- [ ] `tests/test_agent/test_runtime_context_builder.py` — 同上 + 断言新格式
- [ ] `tests/test_agent/test_call_llm_compaction.py` — 同上
- [ ] `tests/test_agent/test_execute_tools_guard.py` — 同上
- [ ] `tests/test_agent/test_execute_tools_todo.py` — 同上
- [ ] `tests/test_agent/test_runtime_guards.py` — 同上
- [ ] `tests/test_agent/test_session_runtime_state.py` — 同上
- [ ] `tests/test_agent/test_session_run_once.py` — 同上
- [ ] `tests/test_agent/test_subagent_step_budget.py` — 同上
- [ ] `tests/test_graph_authorization.py` — 同上
- 测试: `.venv\Scripts\python.exe -m pytest tests/ -q`

## Risks

- **持久化兼容**: 旧 session 的 SQLite 里存的 `todo_state_json` 可能含 `in_progress`/`completed`/`cancelled`。`_load_todo_state` 用 `model_validate`，新 schema 会拒绝旧值。需要在加载时做迁移映射。
- **LLM 行为**: LLM 可能仍输出旧状态值（`in_progress`/`completed`）。`TodoItem.model_validate` 会报错。需要考虑是否在验证层做兼容映射，或依赖 LLM 遵循新的 tool schema。
- **改动面大**: 涉及 ~15 个源文件 + ~10 个测试文件，需要逐个确认不遗漏。
