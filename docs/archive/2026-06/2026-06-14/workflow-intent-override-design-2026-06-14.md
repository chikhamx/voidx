# Spec: Workflow Intent Override

> **Status: Done**
> **Created**: 2026-06-14

## 1. Goal

Make workflow state flexible enough to follow clear user intent without rendering stale intermediate nodes. When the goal resolver says the next workflow should be `plan` or `tdd`, runtime should be able to redirect from an old active precursor such as `brainstorm` directly to the requested workflow.

The intended behavior:

```text
User: "开干" / "直接实现" / "按这个 spec 做"
Resolver: next_workflow="tdd" or "plan"
Runtime: activate that workflow directly
UI: do not show brainstorm -> skip_to_plan -> plan unless that transition is meaningful
```

## 2. Problem

Current reconciliation is a conservative one-edge DAG transition:

```text
active brainstorm + next_workflow=plan -> brainstorm --skip_to_plan--> plan
active brainstorm + next_workflow=tdd  -> no direct edge, so no auto transition
active plan       + next_workflow=tdd  -> plan --approved--> tdd
```

This preserves DAG history, but it makes the user experience noisy:

1. A stale `brainstorm` node remains visible even after the user explicitly says to implement.
2. The agent emits redundant `advance_workflow` calls only to clear old state.
3. UI shows implementation as `brainstorm -> plan -> tdd`, even when the user intent was simply "start TDD now".
4. The resolver can identify `next_workflow`, but reconciliation cannot treat it as an override.

This makes workflow feel like it is "grabbing the steering wheel" instead of helping.

## 3. Design Principles

### 3.1 Resolver intent wins

Priority order:

```text
explicit resolver intent > active workflow state > DAG default
```

When `GoalResolution.next_workflow` is present, it should be treated as a turn-level routing decision, not merely as a request to traverse one DAG edge.

### 3.2 DAG remains useful, but not absolute

The DAG still defines normal lifecycle transitions and default follow-ups:

```text
tdd implemented -> verify
verify failed_bug -> debug
feedback needs_plan -> plan
```

But DAG edges should not force stale precursor nodes to appear when user intent is already explicit.

### 3.3 Workflow gates ask, not deny

Workflow gates should mark sensitive actions as requiring confirmation. They should not produce unconditional runtime denial. This aligns with the permission model:

```text
workflow gate hit -> ask user
user approves -> execute
user denies -> deny
```

### 3.4 Node tools are guidance, not authorization

`WorkflowNode.tools` should describe expected or useful tools for the node. It should not be a hard whitelist. Permission and sandbox policy remain the source of execution authority.

## 4. Proposed Runtime Behavior

### 4.1 Direct override from stale precursor

Add an override path to `reconcile_workflow_runs_for_turn()`:

```text
if goal_resolution.next_workflow is explicit:
    if target is already active:
        no-op
    elif a direct DAG edge exists from an active workflow:
        use the existing edge transition
    elif intent override is allowed:
        satisfy superseded active precursor workflows
        activate target workflow directly
```

The existing direct-edge behavior remains for normal cases. The new override branch only runs when direct transition is not sufficient or would create noisy stale-node transitions.

### 4.2 Override eligibility

Override is allowed when all conditions hold:

| Condition | Reason |
|-----------|--------|
| `next_workflow` is a known workflow node | Prevent invalid runtime state |
| target is not already active | Avoid duplicate activation |
| target is in `{"design-doc", "plan", "tdd"}` initially | Limit blast radius to user-facing routing |
| at least one active workflow is overrideable | Avoid replacing critical verification/debug/review states |
| current goal or turn indicates explicit write/implementation intent | Do not skip design on vague approval |

Initial overrideable active workflows:

```python
OVERRIDEABLE_PRECURSORS = {"brainstorm", "plan", "design-doc"}
OVERRIDE_TARGETS = {"design-doc", "plan", "tdd"}
```

Recommended initial matrix:

| Active workflow | Target | Action |
|-----------------|--------|--------|
| `brainstorm` | `plan` | direct activate `plan`; mark `brainstorm` superseded |
| `brainstorm` | `tdd` | direct activate `tdd`; mark `brainstorm` superseded |
| `plan` | `tdd` | direct activate `tdd`; mark `plan` superseded or use existing edge if explicit approval |
| `design-doc` | `plan` | direct activate `plan`; mark `design-doc` superseded when spec is already accepted |

Non-overrideable examples:

| Active workflow | Target | Reason |
|-----------------|--------|--------|
| `debug` | `tdd` | root cause workflow should not be silently skipped |
| `verify` | `tdd` | failed verification should route through existing evidence |
| `review` | `tdd` | review feedback should go through feedback validation |

### 4.3 Superseded evidence

Reuse `WorkflowRunStatus.SATISFIED` for skipped stale nodes. Add structured evidence:

```python
condition = "superseded_by_intent"
ref = f"auto:turn_reconcile:{source}_superseded_by_{target}"
summary = "User intent explicitly selected target workflow; stale precursor was skipped."
reason = f"next_workflow={target} from goal resolver"
```

This keeps state auditable without pretending the user approved a specific DAG edge such as `skip_to_plan`.

### 4.4 UI rendering

Avoid showing superseded precursor nodes as normal workflow advancement.

Recommended rendering:

```text
Workflow redirected: brainstorm -> tdd
```

Or omit the superseded transition from the visible turn transcript and let the active workflow line show the result:

```text
TDD started. Checking tests first.
```

Implementation can start with state semantics only; UI-specific suppression can follow if needed.

## 5. Resolver Guidance

Update the goal resolver prompt to make `next_workflow` choices sharper:

```text
- If the user explicitly asks to implement an already detailed spec, set next_workflow="tdd".
- If the user asks to turn a spec into an implementation plan, set next_workflow="plan".
- If the user asks to write or revise a design/spec document, set next_workflow="design-doc".
- Do not choose brainstorm when the request already contains an approved or sufficiently detailed spec.
```

The resolver should also preserve `goal.user_requested_write=true` when the user says "开干", "直接实现", "按 spec 做", "开始写", or equivalent direct implementation commands.

## 6. Files To Change

| File | Change |
|------|--------|
| `src/voidx/workflow/reconcile.py` | Add intent override branch after direct-edge reconciliation |
| `src/voidx/agent/goal_resolver.py` | Tighten `next_workflow` guidance for detailed specs and direct implementation |
| `src/voidx/workflow/types.py` | No schema change expected; reuse `SATISFIED` and evidence condition |
| `src/voidx/ui/output/...` | Optional: render `superseded_by_intent` as redirect instead of normal advance |
| `tests/test_workflow_reconcile.py` | Add direct override coverage |
| `tests/test_agent/test_goal_resolver.py` | Add resolver guidance behavior coverage where feasible |
| `tests/test_agent/test_run_loop.py` | Add end-to-end turn initialization coverage for stale `brainstorm` -> direct `tdd` |

## 7. Test Plan

### 7.1 Reconcile unit tests

Add tests for:

1. Active `brainstorm` + `next_workflow="tdd"` + explicit write goal directly activates `tdd`.
2. Active `brainstorm` + `next_workflow="plan"` uses superseded evidence instead of `skip_to_plan` when override mode applies.
3. Active `debug` + `next_workflow="tdd"` does not override.
4. Already active target remains no-op.
5. Unknown `next_workflow` remains ignored.

### 7.2 Run loop tests

Add a test where:

1. `TaskState.workflow_runs` contains active `brainstorm`.
2. structured goal resolver returns `next_workflow="tdd"` with `user_requested_write=true`.
3. initial graph state contains active `tdd` and satisfied `brainstorm`.
4. initial persona is `implement`.

### 7.3 Permission regression

Keep existing coverage that workflow gates ask instead of deny and `WorkflowNode.tools` is not an authorization whitelist.

## 8. Risks

| Risk | Mitigation |
|------|------------|
| Overriding too aggressively skips useful planning | Require explicit `next_workflow` and write/implementation intent |
| Debug/review states get skipped accidentally | Limit overrideable precursors to `brainstorm`, `plan`, `design-doc` |
| UI still shows noisy superseded evidence | Start with correct state; add rendering suppression if transcript remains noisy |
| Resolver chooses `tdd` too often | Keep resolver prompt explicit and tests focused on detailed specs/direct commands |

## 9. Non-Goals

- Do not redesign the entire workflow DAG.
- Do not add a new workflow status unless `SATISFIED + evidence.condition` proves insufficient.
- Do not remove `advance_workflow`; it remains useful for model-driven lifecycle transitions.
- Do not make workflow gates deny tools; gate hits should ask through normal permission flow.

## 10. Acceptance Criteria

- "开干" after an accepted detailed spec can initialize directly into `tdd` without a visible `brainstorm -> skip_to_plan` hop.
- A stale `brainstorm` run is marked satisfied with `condition="superseded_by_intent"` when overridden.
- Non-overrideable workflows such as `debug`, `verify`, and `review` keep existing DAG behavior.
- All workflow reconcile, run loop, and permission regression tests pass.
