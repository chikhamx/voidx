> **Status: Done**

# Goal Resolver 修复与优化

## Context

`goal_resolver` 是每轮对话的意图/目标解析器，通过 LLM structured output 返回 `GoalResolution`，决定当前轮次的 intent、goal 和 workflow 路由。审查发现以下问题需要修复。

## Problems

### P1: plan/goal 模式不应调用 LLM resolver

当前 `InteractionMode.PLAN` 和 `InteractionMode.GOAL` 模式下仍然调用 `resolve_goal_for_turn()`，然后在 `_normalize_resolution` 中强制覆盖 LLM 结果。这浪费一次 LLM 调用，且覆盖逻辑与 resolver 逻辑混在一起，增加理解成本。

- **PLAN 模式**：用户明确要求设计阶段，应直接构造 `GoalResolution(goal=DESIGN, plan.join=brainstorm)`，不需要 LLM 判断
- **GOAL 模式**：用户必须指定目标才能进入 goal 模式，固定从 plan 节点进入（plan 会通过 clarify 问清楚再规划），leave=None 让 workflow 自然流转
### P2: GoalType 不应隐式决定 workflow 路由

当前 `_normalize_resolution` 会在 `plan` 缺失或 `plan.join` 为空时根据 `GoalType` 自动补 `plan.join/leave`。这会把任务语义和 workflow 路由耦合在一起，例如 `FEATURE` 可能需要 `brainstorm`、`plan` 或 `tdd`，不能由 goal type 唯一决定。应统一由显式 `plan.join/leave` 负责路由。

### P3: Resolver prompt 缺少 workflow 描述

当前 prompt 只列出可用 join 值名称（`debug, brainstorm, design, plan, tdd, review, feedback`），但不解释每个 workflow 做什么。LLM 无法根据用户意图准确选择 `plan.join` 和 `plan.leave`。

### P4: goal.desc 缺少指导

prompt 未告诉 LLM `goal.desc` 应该写什么，导致 LLM 返回的 desc 不稳定——有时是用户原文，有时是摘要，有时为空。

### P5: ValidationError 判断使用字符串匹配

`_normalize_resolution` 中用 `fallback_error_type == "ValidationError"` 判断是否走 coding fallback，但 `fallback_error_type` 来自 `type(exc).__name__`，不同库可能有同名异常类。应改用 `isinstance` 检查。

### P6: PLAN 模式 leave 语义错误

`_normalize_resolution` 的 PLAN 分支中，`leave=plan.leave if plan is not None else None` 保留了 LLM 原始 leave 值。但 PLAN 模式强制 `join="brainstorm"`，此时应直接构造 `leave="brainstorm"`，而非沿用 LLM 可能返回的 `"verify"`。

## Design

### D1: plan/goal 模式跳过 LLM 调用

在 `goal_resolver.py` 中新增两个函数：

```python
def resolve_plan_mode(user_text: str, task_state: TaskState) -> GoalResolution:
    """PLAN 模式直接构造结果，不调用 LLM。"""
    desc = task_state.current_goal.desc if task_state.current_goal and task_state.current_goal.desc.strip() else user_text
    return GoalResolution(
        intent=IntentResolution(type=TaskIntent.CODING, desc="plan mode"),
        goal=GoalSpec(type=GoalType.DESIGN, desc=desc),
        plan=PlanResolution(join="brainstorm", leave="brainstorm"),
    )

def resolve_goal_mode(user_text: str, task_state: TaskState) -> GoalResolution:
    """GOAL 模式直接构造结果，不调用 LLM。用户必须指定目标，固定从 plan 进入，plan 会通过 clarify 问清楚再规划。"""
    return GoalResolution(
        intent=IntentResolution(type=TaskIntent.CODING, desc="goal mode"),
        goal=goal,
        plan=PlanResolution(join="plan", leave=None),
    )
```

在 `turn_runner.py` 中，plan/goal 模式下直接调用对应函数，跳过 `resolve_goal_for_turn()`：

```python
if interaction_mode == "plan":
    intent_resolution = resolve_plan_mode(payload.title_text, base_task_state)
elif interaction_mode == "goal":
    intent_resolution = resolve_goal_mode(payload.title_text, base_task_state)
else:
    intent_resolution = await resolve_goal_for_turn(...)
```

### D2: 移除 GoalType → workflow 默认映射

删除 `_default_join_for_goal_type` / `_default_leave_for_goal_type` 作为路由补全来源。`_normalize_resolution` 只校验 `plan.join/leave`：

- `goal` 非空但 `plan` 缺失或 `plan.join` 为空：丢弃 `goal` 和 `plan`，避免产生隐式路由
- `plan.join` 非法或不是允许入口：丢弃 `goal` 和 `plan`
- `plan.leave` 非法：保留 `join`，清空 `leave`
- `plan.join` 合法但 `leave` 为空：保持为空，不再按 `GoalType` 自动补默认 leave

本地 fallback 可以继续基于用户文本做独立 workflow intent heuristic，但不能通过 `GoalType` 映射路由。

### D3: Prompt 添加 workflow 简短描述

在 `_resolver_system_prompt` 中，将可用 join 值从简单列表改为带描述的格式：

```
Available join values:
- brainstorm: Confirm requirements and design, get user approval
- design: Produce a structured document that passes the reader test
- plan: Produce an executable implementation plan, get user approval
- tdd: Complete implementation via TDD cycle, all tests green
- verify: Prove changes reach expected state with reproducible evidence
- review: Initiate structured code review request and collect verdict
- feedback: Verify and implement valid review feedback
- debug: Locate root cause and confirm fix direction
```

描述来源：`BUILTIN_WORKFLOW_NODES` 的 `goal` 字段。

### D4: goal.desc 指导

在 prompt Rules 中添加：

```
- goal.desc: a short summary of the user's request in their language (1-2 sentences).
```

### D5: ValidationError 改用 isinstance

```python
from pydantic import ValidationError as PydanticValidationError

# 在 resolve_goal_for_turn 中：
fallback_is_validation_error = isinstance(exc, PydanticValidationError)

# 在 fallback 判断中：
if fallback_is_validation_error:
    fallback_resolution = _local_coding_fallback(...)
```

### D6: PLAN 模式 leave 使用默认值

此问题随 D1 解决——PLAN 模式不再走 `_normalize_resolution`，直接在 `resolve_plan_mode` 中设置 `leave="brainstorm"`。

## File Changes

| File | Change |
|------|--------|
| `src/voidx/agent/goal_resolver.py` | 新增 `resolve_plan_mode` / `resolve_goal_mode`；prompt 添加 workflow 描述和 goal.desc 指导；ValidationError 改 isinstance；移除 `_normalize_resolution` 中 PLAN/GOAL 分支；移除 GoalType 默认路由补全 |
| `src/voidx/agent/graph/turn_runner.py` | plan/goal 模式下调用新函数，跳过 `resolve_goal_for_turn` |
| `src/voidx/runtime/task_state.py` | 删除 GoalType → workflow 默认映射 helper；`ToolStatePatch` 支持显式 `plan` route |
| `src/voidx/agent/graph/tool_executor.py` | 从 tool `state_patch.plan` 更新 `workflow_route` |
| `tests/test_agent/test_goal_resolver.py` | 更新 plan/goal 模式测试；新增 prompt 描述测试；更新 ValidationError 测试；覆盖无显式 plan 不路由 |

## Risks

- **plan/goal 模式行为变化**：PLAN 模式 leave 从 LLM 决定变为固定 `"brainstorm"`；GOAL 模式固定从 `"plan"` 进入。这是有意为之的简化，但需确认下游 reconcile 逻辑兼容。
- **resolver 严格性提高**：LLM 返回 coding goal 但缺少 `plan.join` 时不再自动路由。prompt 必须清楚要求模型显式返回 `plan.join`。
