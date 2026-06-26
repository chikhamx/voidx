> **Status: Done**

# Checkpoint Workflow-Integrated Options

## Summary

Add "Document first" and "Implement directly" options to the `checkpoint` tool, replacing the current "Approve" label. Integrate checkpoint decisions with the workflow DAG so the LLM is guided to the correct next phase.

## Motivation

Currently `checkpoint` offers three choices: Approve, Modify scope, Reject. When a user wants the plan to proceed but thinks a design document should be produced first (e.g. for non-trivial features), there's no way to express that intent. The user has to reject and re-explain, or approve and then manually request a doc — which the LLM may ignore since the plan is already "approved".

Additionally, checkpoint decisions should guide the LLM toward the correct next phase:

- **needs_doc** (document first) → write an explicit `plan.join="design"` route and hint the LLM to enter the `design` workflow node
- **approved** (implement directly) → hint the LLM to proceed with implementation; the LLM drives workflow advancement itself
- **rejected** → no workflow change, no extra hint (existing behavior preserved)
- **modified** → no workflow change, no extra hint; the LLM sees the user's modified scope and adjusts accordingly

## Design

### Updated options

| Label | Value | Description |
|---|---|---|
| Implement directly | `approved` | Proceed with implementation |
| Document first | `needs_doc` | Approve plan and write a design document before implementation |
| Modify scope | `modified` | Approve with changes to scope |
| Reject | `rejected` | Do not proceed |

### State patch per decision

Each checkpoint decision produces a `ToolStatePatch` that updates runtime state:

| Decision | intent | goal type | Workflow effect |
|---|---|---|---|
| `approved` | CODING | FEATURE | Explicit `plan.join="tdd", plan.leave="verify"` route plus `next_step_hint` tells the LLM to proceed |
| `needs_doc` | CODING | DOC | Explicit `plan.join="design", plan.leave="design"` route plus `next_step_hint` tells the LLM to write the design document |
| `modified` | CODING | FEATURE | Same goal as approved with modified scope text; no workflow push — the LLM reads the user's feedback and adjusts |
| `rejected` | CODING | FEATURE | No workflow change; same goal type as before checkpoint. The LLM sees the rejection and adjusts naturally |

### Workflow routing mechanism

The routing works through the existing goal→plan→reconcile pipeline:

1. `checkpoint` returns `ToolResult` with `metadata.state_patch` containing the new `GoalSpec` and explicit `PlanResolution`
2. `_state_update_from_executed_tools` extracts `state_patch.goal` and sets `update["current_goal"]`
3. `_state_update_from_executed_tools` extracts `state_patch.plan` and sets `update["workflow_route"]`
4. The runtime stores that as `TaskState.workflow_route`
5. Workflow reconciliation uses the explicit route target and activates the matching workflow node

No DAG edge changes are needed. Routing is explicit in `PlanResolution`; `GoalType` remains task semantics and does not imply a workflow route.

Note: `goal_map` in the DAG schema is only used for rendering the overview prompt and validation — it is **not** used by the reconcile pipeline. The actual routing is driven by `PlanResolution.join`.

### LLM guidance via ToolResult.next_step_hint

Add a `next_step_hint: str` field to `ToolResult` (in `src/voidx/tools/base.py`). This field provides explicit guidance to the LLM about what to do next, which is critical because the LLM sees the checkpoint result **within the same turn** and may try to continue working before the workflow reconciliation happens on the next turn.

LLM visibility requirement: `next_step_hint` must be included in the `ToolMessage.content` sent back to the model, not only stored on the Python `ToolResult` object. The current tool executor builds model-visible content from `result.output`, so implementation must append a short `Next step hint: ...` block to the LLM tool message when `result.next_step_hint` is non-empty. UI rendering should continue to use `result.output` only, so the hint guides the model without adding extra user-facing result text.

Only `approved` and `needs_doc` decisions set `next_step_hint`; `modified` and `rejected` leave it empty — the LLM already knows how to handle these cases from the user's feedback or the natural workflow flow.

| Decision | `next_step_hint` |
|---|---|
| `approved` | "Plan approved. Proceed to implementation." |
| `needs_doc` | "Plan approved with doc request. Write a design document before implementing. Use the `document` tool to load a template and write the doc." |
| `modified` | "" (empty — the LLM reads the user's modified scope and adjusts) |
| `rejected` | "" (empty — the LLM knows the user rejected the plan) |

### Changes

**`src/voidx/tools/base.py`**:

1. Add `next_step_hint: str = ""` field to `ToolResult`

**`src/voidx/tools/plan_checkpoint.py`**:

1. Update `PlanCheckpointTool.description` to mention the "Document first" option: change "The user can approve, modify scope, or reject" → "The user can approve, request a design document first, modify scope, or reject"
2. Rename `("Approve", "approved", "Proceed with this plan")` → `("Implement directly", "approved", "Proceed with implementation")`
3. Add `("Document first", "needs_doc", "Approve plan and write a design document before implementation")` to the options list in `execute()`
4. Handle `response.value == "needs_doc"` → `_decision_result(inp, decision="needs_doc")`
5. In `_decision_result()`, add `decision == "needs_doc"` branch:
   - `goal=GoalSpec(type=GoalType.DOC, desc=scope)`
   - `plan=PlanResolution(join="design", leave="design")`
   - `intent=IntentResolution(type=TaskIntent.CODING, desc=scope)` — still a coding task
6. Set `next_step_hint` on the returned `ToolResult` per decision:
   - `approved` → `"Plan approved. Proceed to implementation."`
   - `needs_doc` → `"Plan approved with doc request. Write a design document before implementing. Use the \`document\` tool to load a template and write the doc."`
   - `modified` / `rejected` → `""` (empty)
7. Change `rejected` branch: `goal=GoalSpec(type=GoalType.FEATURE, desc=inp.plan_summary)` instead of `GoalType.DESIGN` — rejected should not push the LLM into a different workflow; it preserves the current state
8. For `modified` decision, keep `goal=FEATURE` with the modified scope text — no intent-aware routing. The LLM sees the user's feedback and adjusts naturally.

**`src/voidx/agent/graph/tool_executor.py`**:

1. When building the `ToolMessage` content for the LLM, append `Next step hint: {result.next_step_hint}` if the hint is non-empty
2. Keep terminal/UI output based on `result.output`, so hints are model guidance rather than extra rendered tool output
3. Extract `state_patch.plan` and update `workflow_route` so workflow routing comes from explicit `plan.join/leave`

No DAG changes are needed. Goal resolver does not need a `GoalType.DOC` mapping because checkpoint supplies the explicit route.

### Tests

**`tests/test_tools/test_basic.py`** (or a new `tests/test_tools/test_plan_checkpoint.py`):

1. **`needs_doc` decision produces correct state patch**: Call `_decision_result(inp, decision="needs_doc")` → assert `state_patch.goal.type == GoalType.DOC`, `state_patch.plan.join == "design"`, `state_patch.intent.type == TaskIntent.CODING`
2. **`needs_doc` decision sets next_step_hint**: Assert the returned `ToolResult.next_step_hint` contains "design document"
3. **`approved` decision sets next_step_hint**: Assert `ToolResult.next_step_hint == "Plan approved. Proceed to implementation."`
4. **`modified` decision has empty next_step_hint**: Assert `ToolResult.next_step_hint == ""`
5. **`rejected` decision has empty next_step_hint**: Assert `ToolResult.next_step_hint == ""`
6. **`rejected` decision keeps goal=FEATURE**: Assert `state_patch.goal.type == GoalType.FEATURE` (not DESIGN — rejected should not push workflow)
7. **`modified` decision keeps goal=FEATURE**: Assert `state_patch.goal.type == GoalType.FEATURE` regardless of modified_scope content
8. **Options list includes "Document first"**: Verify the options passed to `UserInteraction` include `("Document first", "needs_doc", ...)`
9. **Tool description mentions "Document first"**: Assert `PlanCheckpointTool.description` contains "design document" or "Document first"
10. **`next_step_hint` is model-visible**: Execute a tool returning `ToolResult(next_step_hint=...)` and assert the resulting `ToolMessage.content` includes a `Next step hint:` block

### Example flow

**User selects "Document first":**

1. LLM calls `checkpoint` with a plan
2. User clicks "Document first"
3. `checkpoint` returns result with `decision="needs_doc"`, `goal=DOC`, `plan.join="design"`, `next_step_hint="Plan approved with doc request..."`
4. LLM sees the hint in the ToolResult and calls `document` to start writing
5. Workflow state already has `workflow_route.join="design"` from the checkpoint state patch
6. LLM operates under `design` node rules (reader test, etc.)
7. After doc passes reader test, `design` → `plan` → `tdd` continues naturally

**User selects "Implement directly":**

1. LLM calls `checkpoint` with a plan
2. User clicks "Implement directly"
3. `checkpoint` returns result with `decision="approved"`, `goal=FEATURE`, `plan.join="tdd"`, `plan.leave="verify"`, `next_step_hint="Plan approved. Proceed to implementation."`
4. LLM sees the hint and proceeds to implement, driving workflow advancement itself

**User selects "Reject":**

1. `checkpoint` returns result with `decision="rejected"`, `goal=FEATURE`, `next_step_hint=""`
2. No workflow change — the LLM stays in its current workflow state
3. The LLM sees the rejection and adjusts naturally (e.g. revisits the design or gathers more context)

**User selects "Modify scope":**

1. `checkpoint` returns result with `decision="modified"`, `goal=FEATURE`, `next_step_hint=""`
2. The LLM reads the user's modified scope text and adjusts accordingly
3. No workflow push; the LLM decides next steps based on the modified scope
