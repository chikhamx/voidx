> **Status: Done**

# 移除子代理 max_steps 步数硬限制 — 技术设计文档

## Context

子代理（child agent）当前使用 `_subagent_step_budget` 按 mode 计算固定步数上限（4~6步），通过 `for step in range(1, step_budget+1)` 循环执行。这导致：

- 复杂任务（如 implement/debug）6 步往往不够，子代理被迫在步数耗尽时收敛，产出质量差
- 简单任务（如 inspect）4 步可能浪费，但影响不大
- 步数硬限制与已有的 runtime guard（wall_clock、no_progress、repetitive_tools）功能重叠
- 收敛提示（convergence prompt）基于固定步数倒计时，不够灵活

## Goals and Non-Goals

### Goals

- 移除子代理的固定步数硬限制，让子代理自然执行直到任务完成或被 guard 终止
- 保留 runtime guard 作为唯一的终止机制
- UI 不再显示步数信息

### Non-Goals

- 不修改主代理（parent agent）的步数/收敛逻辑
- 不修改 runtime guard 本身的逻辑（wall_clock、no_progress、repetitive_tools 保持现有行为）
- 不修改 `AgentInput` 的参数结构（max_steps 从未暴露给 LLM）

## Architecture

### 变更概览

```
_before_: _subagent_step_budget(mode) → step_budget → for step in range(1, step_budget+1) → guard 终止或步数耗尽
_after_:  while step < 50 → guard 终止或 LLM 无 tool_calls 自然结束
```

核心变化：循环从有限 `for` 改为 50 步安全上限的 `while`，终止条件从"步数耗尽"变为"guard 终止"或"LLM 不再调用工具"。50 步仅为防御性兜底，正常情况下由 guard 终止。

### 终止条件（变更后）

| 终止方式 | 触发条件 | 行为 |
|---------|---------|------|
| 自然完成 | LLM 不调用工具 | 返回文本结果 |
| guard: no_progress | 连续无进展 | terminate → 返回消息 |
| guard: repetitive_tools | 重复调用同一工具 | terminate → 返回消息 |
| guard: wall_clock | 超过时间限制 | terminate → 返回消息 |
| 安全上限 | step >= 50 | 返回最后一条消息（防御性兜底） |
| 异常 | 未捕获异常 | raise |

## Data Model

### TaskState (task_tracker.py)

移除 `max_steps` 和 `step` 字段。UI 不显示步数，tracker 不再跟踪步数。

### SubagentStepStarted (events/schema.py)

移除 `max_steps` 和 `step` 字段。UI 不再显示步数信息。

### SubagentFinished (events/schema.py)

移除 `max_steps` 和 `final_step` 字段。

## API Contract

### run_subagent 签名变更

```python
# before
async def run_subagent(..., step_budget: int, ...) -> str:

# after
async def run_subagent(...) -> str:
```

移除 `step_budget` 参数。50 步安全上限作为内部常量，不暴露给调用方。

### _subagent_step_budget

删除此函数。调用方不再需要计算 step budget。

### convergence.py 变更

| 函数 | 变更 |
|------|------|
| `build_step_hint` | 子代理不再调用（`max_steps` 不传入） |
| `build_final_convergence_prompt` | 子代理不再调用（`has_tool_budget` 逻辑移除） |
| `build_convergence_messages` | 子代理调用时传入 `max_steps=0`，返回空列表，`forced=False` |
| `generate_fallback_summary` | `max_steps=0` 时跳过步数行 |

### subagent.py 循环变更

```python
# before
for step in range(1, step_budget + 1):
    has_tool_budget = step < step_budget - 1
    ...
    if not has_tool_budget and assistant_msg.tool_calls:
        # 强制收敛
        ...
    if not assistant_msg.tool_calls:
        # 自然完成
        ...
# 循环结束 → step_limit

# after
SAFETY_STEP_LIMIT = 50
step = 0
while step < SAFETY_STEP_LIMIT:
    step += 1
    ...
    if not assistant_msg.tool_calls:
        # 自然完成
        ...
    # guard 终止在循环内部处理
    # 无步数耗尽分支
```

移除的逻辑：
- `has_tool_budget = step < step_budget - 1` — 不再需要
- `if not has_tool_budget and assistant_msg.tool_calls:` — 不再强制收敛
- 循环结束后的 `mark_finished(step_budget, "step_limit")` — 不再需要
- `tracker.update(task_id, step=step)` — 不再跟踪步数
- `capture.step_header(step, step_budget, persona)` / `ui_port.ui.step_header(...)` — 不再显示步数

### UI 变更

| 文件 | 变更 |
|------|------|
| `capture.py` | `step_header` 方法移除 `max_n` 参数，不再显示步数 |
| `consumers.py` | `SubagentStepStarted` 处理不再显示 `step/max_steps`；`SubagentFinished` 处理不再显示 `final_step/max_steps` |
| `runtime/ui.py` | `step_header` 方法移除 `max_steps` 参数 |
| `events/schema.py` | `SubagentStepStarted` 移除 `step` 和 `max_steps`；`SubagentFinished` 移除 `final_step` 和 `max_steps` |

### _voidx_graph.py 变更

- 移除 `max_steps = _subagent_step_budget(goal_resolution)` 调用
- 移除 `step_budget=max_steps` 参数传递
- 移除 subagent_start/finish 事件中的 `max_steps` 字段
- 移除 `SubagentFinished` 中的 `max_steps` 和 `final_step` 字段

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| 子代理无限循环 | wall_clock guard 兜底（默认超时终止） |
| guard 未触发 | no_progress guard 兜底（连续无进展终止） |
| 所有 guard 失效 | 50 步安全上限兜底，正常情况下不应触发 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 移除步数限制，保留 50 步兜底 | 1. 提高步数上限到 20~30 2. 让步数可配置 3. 完全无上限 | guard 已经足够，提高上限只是推迟问题；可配置增加复杂度但无实际收益；50 步兜底防止 guard 全部失效的极端情况 |
| UI 不显示步数 | 显示 "step N" 或 "step N/∞" | 步数信息对用户无实际价值，去掉更简洁 |
| 主代理不限制 | 主代理也加 50 步兜底 | 主代理由用户交互控制，不需要步数限制 |
| convergence.py 保留但短路 | 删除整个文件 | convergence 还被主代理使用，不能删除；子代理路径短路即可 |

## Open Questions

无（已全部确认）
