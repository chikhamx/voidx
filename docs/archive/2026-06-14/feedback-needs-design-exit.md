# Feedback 混合出口 — 技术设计文档

> **Status: Done**

## Context

review 节点返回问题后进入 feedback 节点。当前 feedback 只有两个出口：

- `feedback_valid` → tdd（实施变更）
- `feedback_verified` → verify（已实施完，需验证）

实际场景中，review 反馈经常是混合的：部分问题可以直接修，部分需要先分析影响范围或设计方案。例如 review 提出 3 个问题，1/2 是明确的 bug fix，3 是架构级建议需要先讨论方案。

当前 feedback 无法表达"部分实施 + 部分仅分析"的意图，LLM 只能把所有 accepted feedback 都走实施路径，或者用 `done` 手动结束再在新 turn 中处理。

## Goals and Non-Goals

### Goals

- feedback 节点增加 `needs_design` 出口，指向 brainstorm
- feedback 节点增加 `needs_plan` 出口，指向 plan
- 支持同一次 feedback 中"部分实施、部分需设计/计划"的混合场景
- LLM 能通过 `advance_workflow(condition="needs_design")` 或 `advance_workflow(condition="needs_plan")` 自然地路由

### Non-Goals

- 不做多 goal 并发调度（架构级改动，收益不匹配）
- 不改 Goal/TaskState 数据模型（单 goal 约束不变）
- 不改 feedback 节点的 persona（仍然是 implement，因为大部分 feedback 仍需实施）

## Architecture

### DAG 变更

新增两条边：

```
feedback --needs_design--> brainstorm
feedback --needs_plan--> plan
```

完整 DAG 边变更：

```python
# 新增
Edge(
    source="feedback",
    target="brainstorm",
    condition="needs_design",
    label="feedback requires design or analysis",
    description="Use when some feedback items need design exploration or impact analysis rather than direct implementation.",
)
Edge(
    source="feedback",
    target="plan",
    condition="needs_plan",
    label="feedback requires implementation planning",
    description="Use when some feedback items have clear requirements but need a structured implementation plan before coding.",
)
```

#### `needs_design` vs `needs_plan` 的选择

| 条件 | 路由 | 典型场景 |
|------|------|----------|
| 反馈项需要先探索方案、分析影响、讨论取舍 | `needs_design` → brainstorm | 架构级建议、不确定影响范围的改动 |
| 反馈项需求明确但涉及多文件/多步骤，需要先规划实施步骤 | `needs_plan` → plan | 明确的重构任务、多模块联动修改 |
| 反馈项可以直接修 | `feedback_valid` → tdd | 单文件 bug fix、小范围调整 |

### 节点定义变更

#### feedback 节点

**io.output** 新增：

```python
output={
    "changes_made": "根据反馈做的变更",
    "feedback_status": "每条反馈的处理状态(accepted/rejected/deferred)",
    "deferred_items": "需要设计、分析或规划而非直接实施的反馈项",  # 新增
}
```

**workflow** step 6 修改：

```
# 当前
WorkflowStep(order=6, action="Implement valid feedback", description="One coherent item at a time.")

# 修改为
WorkflowStep(order=6, action="Implement valid feedback", description="One coherent item at a time. If an item requires design exploration or impact analysis rather than direct code change, defer it and route via needs_design. If an item has clear requirements but needs a structured implementation plan, route via needs_plan.")
```

**rules** 新增一条：

```python
"If some feedback items need design or analysis rather than direct implementation, implement the actionable items first, then use needs_design to route the remaining items to brainstorm."
"If some feedback items have clear requirements but need a structured implementation plan, use needs_plan to route them to plan."
```

### Policy 变更

`workflow_activations` 不需要改动。`needs_design` 是通过 `advance_workflow` 工具显式触发的 transition，不是自动激活。

`WORKFLOW_TRANSITIONS` 会自动从 DAG edges 计算，无需手动维护。

### Runtime 行为

#### 场景 A：review 提出 3 个问题，1/2 要修，3 要分析

1. review → `review_has_issues` → feedback（自动，已有）
2. feedback 中：
   - 用 todo 列出 3 个问题
   - 修 1、2（implement persona，正常写代码）
   - 对 3 标记为 deferred，在输出中描述
3. 修完 1/2 后，调用 `advance_workflow(condition="needs_design")`
4. feedback 变为 satisfied，brainstorm 被激活
5. brainstorm 中处理问题 3（explore persona，只读分析）
6. brainstorm → `approved` → design-doc 或 `skip_to_plan` → plan 或 `small_change` → tdd

#### 场景 B：review 提出 2 个问题，1 可直接修，2 需要规划实施步骤

1. review → `review_has_issues` → feedback
2. feedback 中：
   - 修问题 1
   - 问题 2 需求明确但涉及多模块，标记 deferred
3. 调用 `advance_workflow(condition="needs_plan")`
4. feedback → satisfied，plan → active
5. plan 中规划问题 2 的实施步骤（plan persona）
6. plan → `approved` → tdd

#### 场景 C：所有 feedback 都可直接修

行为不变，走 `feedback_valid` → tdd。

#### 场景 D：所有 feedback 都只需分析

走 `needs_design` → brainstorm，不经过 tdd。

#### 场景 E：所有 feedback 都需规划

走 `needs_plan` → plan，不经过 tdd 直接进入规划。

#### 多个 active workflow 的 persona 合并

当 feedback satisfied 后 brainstorm 或 plan 激活时，`_persona_for_workflow_runs` 会收集所有 active run 的 personas。实际流程中 feedback satisfied 后只有一个 successor active：
- `needs_design` → brainstorm active，persona 为 `explore`
- `needs_plan` → plan active，persona 为 `plan`
- `feedback_valid` → tdd active，persona 为 `implement`

行为均正确。

## Data Model

无数据模型变更。`WorkflowRunState`、`Goal`、`TaskState` 均不变。

## API Contract

### advance_workflow 新增 conditions

| Condition | Source | Target | Required evidence | Effect |
|-----------|--------|--------|-------------------|--------|
| `needs_design` | feedback | brainstorm | 是 | feedback → satisfied，brainstorm → active |
| `needs_plan` | feedback | plan | 是 | feedback → satisfied，plan → active |

### Workflow exits 变更

feedback 节点的 exits 从：

```
feedback_valid -> tdd; feedback_verified -> verify; done -> end
```

变为：

```
feedback_valid -> tdd; feedback_verified -> verify; needs_design -> brainstorm; needs_plan -> plan; done -> end
```

LLM 在 Current Task State 中看到 feedback active 时，会看到新增的 `needs_design` 和 `needs_plan` 出口。

## Error Handling

| 失败场景 | 处理策略 |
|---------|----------|
| feedback 未 active 时调用 `needs_design`/`needs_plan` | advance_workflow 返回 "no active node" 错误，已有逻辑 |
| feedback 中没有 deferred items 就用 `needs_design`/`needs_plan` | 不阻止，由 LLM 判断。gate 要求 evidence，LLM 需要说明哪些项需要设计/规划 |
| brainstorm/plan 已 active 时 feedback → `needs_design`/`needs_plan` | reconcile 中 `_has_active` 检查会阻止重复激活，已有逻辑 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|----------|----------|
| `needs_design` 指向 brainstorm | 指向 explore 子 agent | brainstorm 有完整的探索+设计流程，explore 只是 persona |
| `needs_plan` 指向 plan | 指向 tdd | plan 有结构化实施规划流程，tdd 直接写代码跳过了规划步骤 |
| 不改 feedback persona | 改为混合 persona | feedback 主体仍是实施，只有少数项需要设计/规划，改 persona 会影响大部分正常流程 |
| 不增加 `needs_inspect` 出口 | 只加 `needs_design`/`needs_plan` | inspect 场景（只分析不设计）也可以走 brainstorm → done，不需要单独出口 |
| 两个出口作为显式 transition | 自动检测 deferred items | 自动检测需要解析 LLM 输出，不可靠；显式 transition 让 LLM 自主判断 |

## Open Questions

- [x] feedback 中修完 1/2 后走 `needs_design`，问题 3 进入 brainstorm。但 brainstorm 的 goal 是"确认需求和设计方案"，而问题 3 可能只是"分析影响范围"不需要设计方案。brainstorm 的 workflow step 2-4（提问、提方案、等批准）可能过重。是否需要一个更轻量的 `explore_only` 节点？
  - **决策：暂不处理。** brainstorm 的 explore persona 天然支持"只分析不设计"——LLM 可以在 step 2-3 中完成分析后直接用 `done` 结束，不必走完所有 step。如果后续发现频繁出现"分析完就被迫走设计流程"的问题，再考虑新增轻量节点。

## Implementation Plan

### Phase 1: DAG 边 + 节点定义

**文件**: `src/voidx/workflow/dag.py`
- `DEFAULT_WORKFLOW_DAG.edges` 新增两条边：`feedback → brainstorm(needs_design)` 和 `feedback → plan(needs_plan)`

**文件**: `src/voidx/workflow/nodes.py`
- `RECEIVING_CODE_REVIEW.io.output` 新增 `deferred_items` 字段
- `RECEIVING_CODE_REVIEW.workflow` step 6 description 更新
- `RECEIVING_CODE_REVIEW.rules` 新增两条规则

### Phase 2: 测试

**文件**: `tests/test_workflow_*.py`
- 验证 `workflow_transitions("feedback")` 包含 `brainstorm` 和 `plan`
- 验证 `workflow_edges("feedback")` 包含 `needs_design` 和 `needs_plan` 条件
- 验证 `advance_workflow(condition="needs_design")` 在 feedback active 时正确激活 brainstorm
- 验证 `advance_workflow(condition="needs_plan")` 在 feedback active 时正确激活 plan
- 验证 feedback exits 在 runtime context 中正确展示

### 不需要修改的文件

- `workflow/policy.py` — `WORKFLOW_TRANSITIONS` 从 DAG 自动计算，`workflow_activations` 不涉及新 condition
- `workflow/runtime.py` — `advance_workflow_states` 通用逻辑已覆盖
- `workflow/reconcile.py` — `_resolve_auto_transition` 通用逻辑已覆盖
- `tools/advance_workflow.py` — `_select_run` 通过 `workflow_edges` 匹配 condition，无需改动
- `agent/runtime_context.py` — `workflow_exit_summaries` 从 DAG 动态读取，无需改动
