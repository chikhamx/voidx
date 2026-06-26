# Spec: 拆分 tool_executor.py

> **Status: Done**

## 背景

`src/voidx/agent/graph/tool_executor.py` 当前 1259 行，承担了工具执行编排的全部职责。核心方法 `execute_tools` 约 452 行（L102–L553），内含两个大闭包（`execute_one` L225–L390、`execute_approved` L392–L434）和一个状态更新闭包（`apply_state_update` L160–L203），可维护性差。

`graph/` 目录已有 24 个 .py 文件，平铺 5 个 `tool_execution_*.py` 会进一步加剧拥挤。采用子包方案将拆分后的模块收纳到 `tool_executor/` 目录中。

## 目标

将 `tool_executor.py` 拆分为子包 `tool_executor/`，内含 6 个职责单一的模块，每个模块 < 350 行。不改变任何运行时行为，纯结构重构。通过 `__init__.py` re-export 保持公开 API 导入路径不变。

## 拆分方案

### 新文件结构

```
src/voidx/agent/graph/
├── tool_executor/              # 子包（原 tool_executor.py 拆分而来）
│   ├── __init__.py             # re-export 公开 API（~15 行）
│   ├── executor.py             # GraphToolExecutor 类 + 编排逻辑（~310 行）
│   ├── types.py                # 共享类型 + 常量（~20 行）
│   ├── ui.py                   # UI 通知逻辑（~120 行）
│   ├── guards.py               # 运行时守卫逻辑（~150 行）
│   ├── workflow.py             # Workflow 状态推进逻辑（~290 行）
│   └── helpers.py              # 通用辅助函数（~330 行）
└── tool_execution.py           # 兼容层（不变，导入路径从 tool_executor 改为 tool_executor.executor）
```

### 0. `types.py` — 共享类型与常量（~20 行）

多个子模块依赖 `_ExecutedTool` dataclass，提取到独立类型文件消除循环依赖。

| 符号 | 来源 | 迁移原因 |
|------|------|----------|
| `_ExecutedTool` | `tool_executor.py` L86 | guards、workflow、helpers 均引用 |
| `ToolResultOk` | `tool_executor.py` L93 | guards 的 `_record_runtime_guard_outcomes` 参数类型 |
| `AGENT_RESULT_PREVIEW_LINES` | `tool_executor.py` L81 | helpers 的 `_agent_result_preview` 引用 |
| `AGENT_RESULT_PREVIEW_CHARS` | `tool_executor.py` L82 | helpers 的 `_agent_result_preview` 引用；`tool_execution.py` re-export |

### 1. `executor.py` — 入口 + 编排（~310 行）

保留：
- `GraphToolExecutor` 类（`__init__` + 精简后的 `execute_tools` + `tool_result_ok` 静态方法）
- `make_context` 闭包（L134–L156，23 行，保留在 `execute_tools` 内部）

`execute_tools` 精简方式：
- `execute_one` 闭包 → UI 通知委托给 `ui` 模块函数，核心执行逻辑（调用 `host.tools.execute_tool`、构建 `ToolMessage`）保留内联
- `execute_approved` 闭包 → 提取为 `_execute_approved_batch()`（移入 `helpers`），`execute_tools` 调用之
- `apply_state_update` 闭包 → 提取为 `_apply_state_update()`（移入 `helpers`），`execute_tools` 调用之

> **闭包解构策略**：`execute_one` 捕获了 `host`（43 次引用）、`ctx`（6 次）、`workspace`（7 次）、`display_policy`（2 次）、`result_ok`（2 次）、`session_id`（2 次）等外部变量。UI 通知函数通过参数显式传入所需值（`host`、`tc`、`display_policy`、`tool_node` 等），不依赖闭包捕获。核心执行逻辑（L266–L390）因与 `host`、`ctx`、`workspace` 深度耦合，保留为 `execute_tools` 内的闭包。

### 2. `ui.py` — UI 通知（~120 行）

从 `execute_one` 中提取的 UI 分支逻辑：

| 函数 | 职责 | 参数 |
|------|------|------|
| `notify_tool_started(host, tc, display_policy)` | ToolStarted / dock.start_tool / ui.tool_call 三路分支 | 返回 `tool_node` 供后续使用 |
| `notify_tool_result(host, tc, result, ok, elapsed, display_policy, tool_node)` | ToolFinished / dock.finish / ui.tool_done 三路分支 | |
| `notify_tool_diff(host, result, tool_event_id, tool_node)` | diff 渲染三路分支 | |
| `notify_tool_failure(host, tc, result, display_mode, tool_event_id, tool_node)` | 隐藏工具失败通知三路分支 | |
| `notify_tool_text_output(host, output, tid, tool_event_id, tool_node, display_policy, ok)` | 非 diff 文本输出三路分支 | |

每个函数内部处理 `via_events()` / `dock.active` / fallback 三条路径，消除 `execute_one` 中的重复。

> **设计约束**：所有函数均为 `async`（`via_events()` 路径需要 `await`）。`host` 参数类型为 `object`，函数内部通过 `host._ui` 访问 UI 子系统，与现有闭包中的访问方式一致。

### 3. `guards.py` — 运行时守卫（~150 行）

| 函数 | 行数 | 备注 |
|------|------|------|
| `_runtime_guard_state` | 6 行 | |
| `_split_runtime_guard_blocked_calls` | 12 行 | |
| `_runtime_guard_blocked_tool` | 22 行 | 返回 `_ExecutedTool`，从 `types` 导入 |
| `_restore_runtime_guard_blocked_results` | 16 行 | 参数/返回值含 `_ExecutedTool` |
| `_runtime_guard_tool_messages` | 10 行 | |
| `_record_runtime_guard_outcomes` | 46 行 | 调用同模块的 `_submit_guard_guidance`、`_emit_wall_clock_status`、`_latest_action_from_summary` |
| `_emit_wall_clock_status` | 9 行 | |
| `_latest_action_from_summary` | 4 行 | |
| `_submit_guard_guidance` | 10 行 | |

**依赖**：从 `.types` 导入 `_ExecutedTool`、`ToolResultOk`。无其他内部模块依赖。

### 4. `workflow.py` — Workflow 状态推进（~290 行）

| 函数 | 行数 | 备注 |
|------|------|------|
| `_state_update_from_executed_tools` | 69 行 | 调用 `_merge_workflow_runs_for_state`、`_explicit_advance_route_limited_runs`、`_auto_advance_from_executed`、`_advance_auto_events_for_route` |
| `_inline_compaction_messages` | 25 行 | 含内部闭包 `use_submitted_summary` |
| `_inline_compaction_summary` | 10 行 | |
| `_auto_advance_from_executed` | 12 行 | |
| `_explicit_advance_route_limited_runs` | 35 行 | |
| `_advance_auto_events_for_route` | 30 行 | |
| `_auto_event_satisfies_route_terminal` | 14 行 | |
| `_auto_event_should_stop_after_transition` | 9 行 | |
| `_satisfy_workflow_without_transition` | 25 行 | |
| `_terminal_workflow_completed` | 27 行 | |
| `_merge_workflow_runs_for_state` | 11 行 | |

**依赖**：从 `.types` 导入 `_ExecutedTool`。无其他内部模块依赖（`_state_update_from_executed_tools` 调用的函数均在本模块内）。

### 5. `helpers.py` — 通用辅助（~330 行）

| 函数 | 行数 | 备注 |
|------|------|------|
| `_invalidate_tui` | 5 行 | 被 `_apply_state_update` 调用 |
| `_dedupe_repeated_read_calls` | 19 行 | |
| `_read_call_key` | 3 行 | |
| `_restore_deduped_read_results` | 27 行 | 构造 `_ExecutedTool` 实例 |
| `_parallel_subagent_limit` | 9 行 | |
| `_agent_result_preview` | 23 行 | 引用 `AGENT_RESULT_PREVIEW_*` 常量 |
| `_is_barrier_tool` | 2 行 | |
| `_split_at_first_barrier` | 5 行 | |
| `_blocked_after_barrier_messages` | 15 行 | |
| `_authorize_tool_calls` | 26 行 | |
| `_make_interact_callback` | 32 行 | 调用 `_is_tuple_options`、`_other_choice_value` |
| `_is_tuple_options` | 3 行 | |
| `_other_choice_value` | 13 行 | 引用 `_OTHER_VALUE_PREFIX` |
| `_task_state_for_state` | 11 行 | |
| `_goal_for_state` | 11 行 | |
| `_todo_state_for_state` | 11 行 | |
| `_workflow_runs_for_state` | 10 行 | |
| `_apply_state_update` | ~44 行 | 从 `execute_tools` 闭包提取；调用 `_invalidate_tui`、`_task_state_for_state`、`_todo_state_for_state`、`_goal_for_state`、`_workflow_runs_for_state` |
| `_execute_approved_batch` | ~43 行 | 从 `execute_tools` 闭包提取；调用 `_split_runtime_guard_blocked_calls`、`_dedupe_repeated_read_calls`、`_restore_deduped_read_results`、`_restore_runtime_guard_blocked_results`、`_parallel_subagent_limit` |

**常量**：
- `_OTHER_VALUE_PREFIX`（L80）：被 `_other_choice_value` 引用，随函数迁移

**依赖**：
- 从 `.types` 导入 `_ExecutedTool`、`ToolResultOk`、`AGENT_RESULT_PREVIEW_*`
- 从 `.guards` 导入 `_split_runtime_guard_blocked_calls`、`_restore_runtime_guard_blocked_results`（`_execute_approved_batch` 使用）

> **模块体积说明**：helpers 是最大的子模块（~330 行），但内部函数按职责可分三组：执行辅助（dedupe/barrier/authorize/limit，~106 行）、交互回调（interact/choice，~48 行）、状态提取（task_state/goal/todo/workflow_runs，~43 行）+ 两个提取闭包（~87 行）。状态提取组仅 43 行，不足以独立成模块；交互回调与 `_make_interact_callback` 紧密耦合。保持单模块更利于维护。

## `__init__.py` — Re-export 策略

```python
"""Tool execution component for the agent graph."""

from .executor import GraphToolExecutor
from .types import AGENT_RESULT_PREVIEW_CHARS, AGENT_RESULT_PREVIEW_LINES, ToolResultOk, _ExecutedTool
from .helpers import _agent_result_preview, _make_interact_callback
```

这确保以下导入路径在拆分后仍然有效：

```python
from voidx.agent.graph.tool_executor import GraphToolExecutor          # 公开 API
from voidx.agent.graph.tool_executor import _ExecutedTool              # 内部符号
from voidx.agent.graph.tool_executor import AGENT_RESULT_PREVIEW_CHARS  # 常量
```

> **注意**：`tool_executor` 从文件模块变为包，`tool_executor.__file__` 的值会改变。如果任何代码依赖 `__file__` 定位资源文件，需单独处理。经检查，当前代码无此依赖。

## 导入关系

```
tool_executor/types.py
  └── (无内部依赖)

tool_executor/guards.py
  └── from .types import _ExecutedTool, ToolResultOk

tool_executor/ui.py
  └── (无内部依赖)

tool_executor/workflow.py
  └── from .types import _ExecutedTool

tool_executor/helpers.py
  ├── from .types import _ExecutedTool, ToolResultOk, AGENT_RESULT_PREVIEW_*
  └── from .guards import _split_runtime_guard_blocked_calls, _restore_runtime_guard_blocked_results

tool_executor/executor.py
  ├── from .types import _ExecutedTool, ToolResultOk, AGENT_RESULT_PREVIEW_*
  ├── from .ui import ...
  ├── from .guards import ...
  ├── from .workflow import ...
  └── from .helpers import ...

tool_executor/__init__.py
  └── from .executor, .types, .helpers import ...  (re-export)
```

无循环依赖。`executor.py` 作为顶层编排模块，依赖所有子模块；子模块之间仅 `helpers` → `guards` 有单向依赖。

## 外部导入路径变更

### `tool_execution.py` 兼容层

当前导入：
```python
from voidx.agent.graph.tool_executor import (
    AGENT_RESULT_PREVIEW_CHARS,
    GraphToolExecutor,
    _agent_result_preview,
    _make_interact_callback,
    todo_updated_event,
)
```

拆分后 `tool_executor` 变为包，`__init__.py` re-export 了这些符号，**此文件无需修改**。

### 测试文件

以下测试直接导入内部函数，拆分后需更新导入路径：

| 测试文件 | 当前导入 | 拆分后应改为 |
|----------|----------|-------------|
| `tests/test_tools/test_make_interact_callback.py` | `from voidx.agent.graph.tool_executor import _make_interact_callback` | `from voidx.agent.graph.tool_executor.helpers import _make_interact_callback` |
| `tests/test_tools/test_state_update_from_executed_tools.py` | `from voidx.agent.graph.tool_executor import _state_update_from_executed_tools, _ExecutedTool` | `from voidx.agent.graph.tool_executor.workflow import _state_update_from_executed_tools`<br>`from voidx.agent.graph.tool_executor.types import _ExecutedTool` |
| `tests/test_tools/test_interactive_tools_clarify.py` | `from voidx.agent.graph.tool_executor import _ExecutedTool, _state_update_from_executed_tools` | 同上 |
| `tests/test_runtime/test_goal_resolution_refactor.py` | `from voidx.agent.graph.tool_executor import _ExecutedTool, _state_update_from_executed_tools` | 同上 |

> **替代方案**：在 `__init__.py` 中 re-export 所有被外部引用的内部符号，测试无需修改。但 re-export 列表需随拆分维护，且掩盖了实际的模块归属。推荐直接更新测试导入路径。

### 其他外部导入

| 文件 | 导入 | 影响 |
|------|------|------|
| `src/voidx/agent/graph/core.py` | `GraphToolExecutor` | 无影响（`__init__.py` re-export） |
| `src/voidx/agent/graph/contracts.py` | `GraphToolExecutor`（TYPE_CHECKING） | 无影响 |

## 不变项

- `GraphToolExecutor` 公开 API 签名不变
- 所有运行时行为不变
- `from voidx.agent.graph.tool_executor import GraphToolExecutor` 路径不变
- `_ExecutedTool`、`ToolResultOk`、`AGENT_RESULT_PREVIEW_*` 通过 `__init__.py` re-export 保持可从原路径导入

## 验证

```bash
# 全量测试（相关测试散布在 test_agent/ 和 test_tools/ 下，文件名不一定含 tool_executor）
.venv/bin/python -m pytest tests/ -v

# 导入检查 — 公开 API（路径不变）
.venv/bin/python -c "from voidx.agent.graph.tool_executor import GraphToolExecutor; print('OK')"

# 导入检查 — re-export 兼容
.venv/bin/python -c "from voidx.agent.graph.tool_executor import _ExecutedTool, ToolResultOk, AGENT_RESULT_PREVIEW_CHARS; print('OK')"

# 导入检查 — 子模块可导入
.venv/bin/python -c "from voidx.agent.graph.tool_executor.types import _ExecutedTool; print('types OK')"
.venv/bin/python -c "from voidx.agent.graph.tool_executor.guards import _runtime_guard_state; print('guards OK')"
.venv/bin/python -c "from voidx.agent.graph.tool_executor.workflow import _state_update_from_executed_tools; print('workflow OK')"
.venv/bin/python -c "from voidx.agent.graph.tool_executor.helpers import _make_interact_callback; print('helpers OK')"
.venv/bin/python -c "from voidx.agent.graph.tool_executor.ui import notify_tool_started; print('ui OK')"
```

## 实施顺序

1. 创建 `tool_executor/` 目录和 `__init__.py`（re-export 公开 API），删除原 `tool_executor.py`
2. 创建 `types.py`，迁移 `_ExecutedTool`、`ToolResultOk`、`AGENT_RESULT_PREVIEW_*`
3. 创建 `helpers.py`，迁移纯函数（不含 `_apply_state_update` 和 `_execute_approved_batch`）
4. 创建 `guards.py`，迁移守卫函数
5. 创建 `workflow.py`，迁移 workflow 函数
6. 创建 `ui.py`，提取 UI 通知逻辑，精简 `execute_one` 闭包
7. 创建 `executor.py`，迁移 `GraphToolExecutor` 类，从 `execute_tools` 提取 `apply_state_update` → `_apply_state_update()` 和 `execute_approved` → `_execute_approved_batch()` 到 `helpers`
8. 更新测试文件导入路径
9. 运行全量测试验证

每步完成后运行 `pytest tests/ -x` 确认无回归，再进行下一步。
