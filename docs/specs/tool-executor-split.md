# Spec: 拆分 tool_executor.py

> **Status: In Progress**

## 背景

`src/voidx/agent/graph/tool_executor.py` 当前 1216 行，承担了工具执行编排的全部职责。核心方法 `execute_tools` 约 430 行，内含两个大闭包（`execute_one`、`execute_approved`）和一个状态更新闭包（`apply_state_update`），可维护性差。

## 目标

将 `tool_executor.py` 拆分为 4-5 个职责单一的模块，每个模块 < 350 行。不改变任何运行时行为，纯结构重构。

## 拆分方案

### 新文件结构

```
src/voidx/agent/graph/
├── tool_executor.py          # 入口 + GraphToolExecutor 类（精简后 ~200 行）
├── tool_execution_ui.py      # UI 通知逻辑（新建，~200 行）
├── tool_execution_guards.py  # 运行时守卫逻辑（新建，~200 行）
├── tool_execution_workflow.py # Workflow 状态推进逻辑（新建，~250 行）
└── tool_execution_helpers.py # 通用辅助函数（新建，~200 行）
```

### 1. `tool_executor.py` — 入口 + 编排（~200 行）

保留：
- `GraphToolExecutor` 类（`__init__` + 精简后的 `execute_tools`）
- `_ExecutedTool` dataclass
- `ToolResultOk` 类型别名
- `AGENT_RESULT_PREVIEW_LINES` / `AGENT_RESULT_PREVIEW_CHARS` 常量

`execute_tools` 精简方式：
- `execute_one` 闭包 → 调用 `tool_execution_ui.notify_started()` + `tool_execution_ui.notify_result()` + 核心执行逻辑内联
- `execute_approved` 闭包 → 调用 `_execute_approved_batch()`（从 `tool_execution_helpers` 导入）
- `apply_state_update` 闭包 → 调用 `_apply_state_update()`（从 `tool_execution_helpers` 导入）

### 2. `tool_execution_ui.py` — UI 通知（~200 行）

从 `execute_one` 中提取的 UI 分支逻辑：

| 函数 | 职责 |
|------|------|
| `notify_tool_started(host, tc, display_policy)` | ToolStarted / dock.start_tool / ui.tool_call 三路分支 |
| `notify_tool_result(host, tc, result, ok, elapsed, display_policy, tool_node)` | ToolFinished / dock.finish / ui.tool_done 三路分支 |
| `notify_tool_diff(host, result, tool_event_id, tool_node)` | diff 渲染三路分支 |
| `notify_tool_failure(host, tc, result, display_mode, tool_event_id, tool_node)` | 隐藏工具失败通知三路分支 |
| `notify_tool_text_output(host, output, tid, tool_event_id, tool_node, display_policy, ok)` | 非 diff 文本输出三路分支 |

每个函数内部处理 `via_events()` / `dock.active` / fallback 三条路径，消除 `execute_one` 中的重复。

### 3. `tool_execution_guards.py` — 运行时守卫（~200 行）

从 `tool_executor.py` 底部提取：

| 函数 | 原行号 |
|------|--------|
| `_runtime_guard_state` | L535 |
| `_split_runtime_guard_blocked_calls` | L543 |
| `_runtime_guard_blocked_tool` | L557 |
| `_restore_runtime_guard_blocked_results` | L581 |
| `_runtime_guard_tool_messages` | L599 |
| `_record_runtime_guard_outcomes` | L611 |
| `_emit_wall_clock_status` | L659 |
| `_latest_action_from_summary` | L672 |
| `_submit_guard_guidance` | L678 |

### 4. `tool_execution_workflow.py` — Workflow 状态推进（~250 行）

| 函数 | 原行号 |
|------|--------|
| `_state_update_from_executed_tools` | L690 |
| `_inline_compaction_messages` | L758 |
| `_inline_compaction_summary` | L785 |
| `_auto_advance_from_executed` | L797 |
| `_explicit_advance_route_limited_runs` | L811 |
| `_advance_auto_events_for_route` | L846 |
| `_auto_event_satisfies_route_terminal` | L878 |
| `_auto_event_should_stop_after_transition` | L894 |
| `_satisfy_workflow_without_transition` | L905 |
| `_terminal_workflow_completed` | L987 |
| `_merge_workflow_runs_for_state` | L1015 |

### 5. `tool_execution_helpers.py` — 通用辅助（~200 行）

| 函数 | 原行号 |
|------|--------|
| `_dedupe_repeated_read_calls` | L932 |
| `_read_call_key` | L953 |
| `_restore_deduped_read_results` | L958 |
| `_parallel_subagent_limit` | L1028 |
| `_agent_result_preview` | L1039 |
| `_is_barrier_tool` | L1064 |
| `_split_at_first_barrier` | L1068 |
| `_blocked_after_barrier_messages` | L1075 |
| `_authorize_tool_calls` | L1092 |
| `_make_interact_callback` | L1120 |
| `_other_choice_value` | L1150 |
| `_task_state_for_state` | L1160 |
| `_goal_for_state` | L1173 |
| `_todo_state_for_state` | L1186 |
| `_workflow_runs_for_state` | L1199 |
| `_active_workflow_names` | L1211 |
| `_apply_state_update` | 从 `execute_tools` 闭包提取 |
| `_execute_approved_batch` | 从 `execute_tools` 闭包提取 |

## 导入关系

```
tool_executor.py
  ├── from .tool_execution_ui import ...
  ├── from .tool_execution_guards import ...
  ├── from .tool_execution_workflow import ...
  └── from .tool_execution_helpers import ...

tool_execution_workflow.py
  └── from .tool_execution_helpers import ...  (_merge_workflow_runs_for_state 等)

tool_execution_guards.py
  └── (无内部依赖)

tool_execution_ui.py
  └── (无内部依赖)

tool_execution_helpers.py
  └── (无内部依赖)
```

无循环依赖。

## 不变项

- 所有公开 API（`GraphToolExecutor`, `ToolResultOk`）签名不变
- 所有运行时行为不变
- `_ExecutedTool` dataclass 保留在 `tool_executor.py`，其他模块通过导入使用
- 外部模块对 `tool_executor` 的导入路径不变

## 验证

```bash
# 现有测试应全部通过
.venv/bin/python -m pytest tests/ -v -k "tool_executor or tool_execution"

# 导入检查
.venv/bin/python -c "from voidx.agent.graph.tool_executor import GraphToolExecutor; print('OK')"
```

## 实施顺序

1. 创建 `tool_execution_helpers.py`，迁移纯函数，更新 `tool_executor.py` 导入
2. 创建 `tool_execution_guards.py`，迁移守卫函数，更新导入
3. 创建 `tool_execution_workflow.py`，迁移 workflow 函数，更新导入
4. 创建 `tool_execution_ui.py`，提取 UI 通知逻辑，精简 `execute_one`
5. 从 `execute_tools` 提取 `apply_state_update` 和 `execute_approved` 为独立函数
6. 运行全量测试验证
