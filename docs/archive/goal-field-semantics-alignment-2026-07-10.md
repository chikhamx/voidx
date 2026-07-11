> **Status: Done** — Archived on 2026-07-10.

---
name: goal-field-semantics-alignment
display_name: Goal Field Semantics Alignment
description: 对齐 workflow/checkpoint/clarify 三处 goal 字段的语义描述与 resolver prompt 一致，并移除 clarify 的 goal 写入能力
doc_type: implementation-spec
audience: llm
---

# Goal Field Semantics Alignment — Implementation Spec

## Objective

统一 workflow 工具、checkpoint 工具、clarify 工具三处对 `goal` 字段的语义描述，使其与 goal resolver prompt 中的定义一致；移除 clarify 工具对 `current_goal` 的写入能力。

## Source of Truth

| Source | Path / Link | Notes |
|--------|-------------|-------|
| Resolver prompt | `src/voidx/agent/goal_resolver.py:260` | goal 语义的权威定义 |
| GoalSpec model | `src/voidx/runtime/task_state.py:22-33` | `desc` 字段，max 120 chars，`label` property |
| Workflow tool | `src/voidx/tools/workflow.py:50-57` | `WorkflowInput.goal` 字段描述 |
| Checkpoint tool | `src/voidx/tools/checkpoint.py:175-217` | `_decision_result` 中四处 `GoalSpec(desc=...)` |
| Clarify tool | `src/voidx/tools/clarify.py:98-121` | `_infer_state_patch` 中的 goal 写入 |
| State patch apply | `src/voidx/agent/graph/tool_executor/workflow.py:72-73` | `patch.goal` → `update["current_goal"]` |
| State patch apply | `src/voidx/agent/graph/tool_executor/helpers.py:371-375` | `update["current_goal"]` → `host._task_state` |

## Current Behavior

### Resolver prompt 中的 goal 定义（权威基准）

`goal_resolver.py:260`:
> Stable overall objective for the current task. Keep it short, sharp, and clear. Verb-first is preferred. Summarize the user's intent without explicit details. Never null or empty.

### Workflow tool 的 goal 字段

`workflow.py:50-57` — `WorkflowInput.goal`:
> One-sentence goal of the current workflow. Required for 'enter'. Optional retarget for 'advance'. Ignored for 'done'.

**问题**：描述说 "goal of the current workflow"，但实际 `_effective_goal()`（`workflow.py:457-465`）的三级回退 `input > run.goal > ctx.goal_target` 意味着这个 goal 最终会写入 `TaskState.current_goal`（经 `_success()` → `ToolStatePatch.goal` → `helpers.py:371-375`）。它是**任务级 goal**，不是 workflow 级 goal。

### Checkpoint tool 的 goal 写入

`checkpoint.py:175-217` — `_decision_result` 中四个 decision 分支：

| Decision | goal 来源 | 也写 plan/workflow? |
|----------|----------|---------------------|
| approved | `inp.plan_summary` | 是 (tdd→verify) |
| needs_doc | `inp.plan_summary` | 是 (design→design) |
| modified | `modified_scope` 或 `inp.plan_summary` | 否 |
| rejected | `inp.plan_summary` | 否 |

**问题**：rejected 分支也写 goal，意味着用户拒绝计划时 `current_goal` 被更新为 `plan_summary`——这不合理，拒绝计划不应改 goal。

### Clarify tool 的 goal 写入

`clarify.py:98-121` — `_infer_state_patch`:
```python
goal_modes = {"inspect", "design", "review", "implement", "debug"}
return ToolStatePatch(
    intent=IntentResolution(type=intent_map[normalized]),
    goal=GoalSpec(desc=answer) if normalized in goal_modes else None,
)
```

**问题**：用户回答 "implement" 等词时，回答本身被当作 goal 写入 `current_goal`。但 clarify 的职责是澄清歧义，不应修改任务目标。goal 应由 resolver 或用户显式设置（`/goal`）。

## Target Behavior

1. **Clarify 不再写入 goal**：`_infer_state_patch` 只推断 intent，不设置 goal。goal 保持 resolver 设定的值。
2. **Workflow goal 字段描述对齐 resolver**：描述改为与 resolver prompt 一致的语义——"Stable overall objective for the current task"，而非 "goal of the current workflow"。
3. **Checkpoint rejected 不写 goal**：rejected 分支不设置 `GoalSpec`，保持 `current_goal` 不变。其余三个分支（approved/needs_doc/modified）继续写 goal，因为它们代表用户对计划的确认或修改，是合理的 goal 更新点。

## Files to Change

| Path | Change Type | Required Change | Do Not Change |
|------|-------------|-----------------|---------------|
| `src/voidx/tools/clarify.py` | modify | 移除 `_infer_state_patch` 中的 goal 写入，只保留 intent 推断 | 保留 intent 推断逻辑 |
| `src/voidx/tools/workflow.py` | modify | 更新 `WorkflowInput.goal` 的 description 对齐 resolver 语义 | 不改 `_effective_goal` 回退逻辑 |
| `src/voidx/tools/checkpoint.py` | modify | rejected 分支移除 `goal=GoalSpec(...)` | 其他三个分支不变 |
| `src/tests/test_tools/test_infer_state_patch.py` | modify | 移除 goal 断言，改为断言 `patch.goal is None` | 保留 intent 断言 |
| `src/tests/test_tools/test_plan_checkpoint.py` | modify | rejected 场景改为断言 goal 不存在 | 其他场景断言不变 |

## Invariants

- `GoalSpec` model 不变（`desc` 字段，max 120 chars，`label` property）。
- `ToolStatePatch.goal` 字段不变（仍为 `GoalSpec | None`）。
- `_apply_state_update` 中 `current_goal` 的写入路径不变。
- Resolver prompt 中的 goal 定义不变（它是权威基准）。
- Workflow tool 的 `_effective_goal` 三级回退逻辑不变。
- Checkpoint 的 approved/needs_doc/modified 三个分支的 goal 写入不变。

## Implementation Requirements

### Functional Requirements

- [ ] `clarify._infer_state_patch` 返回的 `ToolStatePatch` 中 `goal` 永远为 `None`（即 `model_fields_set` 中不含 `"goal"`）。
- [ ] `WorkflowInput.goal` 的 description 包含 "Stable overall objective" 或等价表述，与 resolver prompt 一致。
- [ ] `checkpoint._decision_result` 的 rejected 分支不设置 `goal` 字段。
- [ ] checkpoint approved/needs_doc/modified 分支的 goal 写入行为不变。

### Error Handling

- [ ] N/A — 无新增错误路径。

### Data / Migration Requirements

- [ ] N/A — 运行时状态无持久化格式变更。

### API / Compatibility Requirements

- [ ] `ToolStatePatch` 的 schema 不变。
- [ ] `WorkflowInput` 的 schema 不变（只改 description）。
- [ ] `ClarifyResult` 的 schema 不变。

## Edge Cases

| Case | Required Behavior | Verification |
|------|-------------------|--------------|
| Clarify 回答 "implement" | intent→CODING，goal 不变 | `test_infer_state_patch` 断言 `patch.goal is None` |
| Clarify 回答 "general" | intent→GENERAL，goal 不变 | `test_infer_state_patch` 断言 `patch.goal is None` |
| Clarify 回答 "blue"（无匹配） | patch 为 None | 现有测试 `test_no_match_returns_none` |
| Checkpoint rejected | goal 不在 state_patch 中 | `test_plan_checkpoint` 断言 `"goal" not in patch` |
| Checkpoint approved | goal = plan_summary | 现有测试不变 |
| Checkpoint modified | goal = modified_scope | 现有测试不变 |

## Forbidden Changes

- Do not modify `GoalSpec` model.
- Do not modify `ToolStatePatch` model.
- Do not modify `_apply_state_update` or `_state_update_from_executed_tools`.
- Do not modify resolver prompt.
- Do not modify `_effective_goal` three-tier fallback.
- Do not change checkpoint approved/needs_doc/modified branches' goal writing.
- Do not add new dependencies.

## Tests

| Test Level | Command | Expected Result |
|------------|---------|-----------------|
| Focused | `./test.py --backend -- src/tests/test_tools/test_infer_state_patch.py -v` | All pass, goal assertions updated |
| Focused | `./test.py --backend -- src/tests/test_tools/test_plan_checkpoint.py -v` | All pass, rejected goal assertion updated |
| Regression | `./test.py --backend -- src/tests/test_tools/test_workflow_tool.py -v` | All pass, no behavior change |
| Regression | `./test.py --backend -- src/tests/test_tools/test_state_update_from_executed_tools.py -v` | All pass |
| Regression | `./test.py --backend -- src/tests/test_tools/test_tool_state_patch.py -v` | All pass |

## Definition of Done

- [ ] Clarify `_infer_state_patch` 不再写入 goal。
- [ ] Workflow `goal` 字段 description 与 resolver 语义对齐。
- [ ] Checkpoint rejected 分支不写 goal。
- [ ] 所有 focused + regression 测试通过。
- [ ] 无无关文件变更。
