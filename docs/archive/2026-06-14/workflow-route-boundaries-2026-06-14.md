# Workflow Route Boundaries Design

> **Status: Done**

Date: 2026-06-14

## Goal

Replace the overloaded `GoalResolution.next_workflow` field with explicit
workflow route boundaries:

```python
workflow_start: str | None
workflow_end: str | None
```

The resolver should describe both where this user turn starts in the workflow
DAG and where automatic progression must stop. This lets runtime distinguish
"review only" from "review and fix issues" before the first main-agent LLM call.

## Problem

`next_workflow` only names a local next node. It cannot express a full turn
route.

Two user requests can share the same start node but require different stopping
behavior:

| User request | Start | End | Expected behavior |
|---|---|---|---|
| "review this" | `review` | `review` | Run review and stop with findings. |
| "review this and fix issues" | `review` | `verify` | Run review, validate feedback, implement fixes, and verify. |

With only `next_workflow`, runtime lacks the original route intent when
`review_has_issues` is auto-detected. It can activate `feedback`, but it cannot
know whether to wait for user approval or continue into `feedback -> tdd`.

This also makes stale workflow reconciliation awkward. `next_workflow` sometimes
means "the next DAG edge", sometimes "override stale state", and sometimes "the
workflow that should be active now".

## Design Principles

### Separate Meaning From Route

`Goal` remains the user-intent semantic layer:

- goal type, such as `review`, `feature`, `bugfix`, `inspect`;
- target or scope, such as a file, PR, current diff, or described task;
- whether the user explicitly requested write behavior;
- whether confirmation is needed.

`workflow_start` and `workflow_end` are the execution-route layer:

- `workflow_start` says which workflow node should become active first;
- `workflow_end` says which workflow node is the terminal boundary for automatic
  progression in this turn.

`workflow_runs` remain the runtime-state layer.

### Route Boundaries Are Policy, Not Progress

The resolver does not claim that a workflow has completed. It only declares the
allowed route for this turn. Runtime and tools still produce actual
`WorkflowRunState` evidence.

### DAG Edges Still Matter

Automatic progression may only follow known DAG edges unless an existing
explicit override rule allows stale precursor replacement. `workflow_start` and
`workflow_end` do not permit arbitrary jumps through the DAG.

## Schema

Update `GoalResolution`:

```python
class GoalResolution(BaseModel):
    intent: TaskIntent = TaskIntent.CODING
    goal: Goal | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)
    reason: str = ""
    workflow_start: str | None = None
    workflow_end: str | None = None
```

Normalization rules:

- unknown workflow names normalize to `None`;
- if `workflow_end` is set but `workflow_start` is absent, infer
  `workflow_start` from `goal.type` when possible;
- if `workflow_start` is set and `workflow_end` is absent, default
  `workflow_end=workflow_start` unless the user explicitly requested automatic
  follow-through;
- `next_workflow` is not part of the schema and is not used as a fallback.

## Resolver Guidance

The resolver prompt should instruct:

```text
- Set workflow_start to the first workflow node that should run for this turn.
- Set workflow_end to the workflow node where automatic workflow progression
  must stop.
- Use workflow_start="review", workflow_end="review" for review-only requests.
- Use workflow_start="review", workflow_end="verify" only when the user
  explicitly asks to fix, implement, apply, or continue after review findings.
- Do not infer write permission from "review", "look at", "check", "分析",
  or "看看".
- Set goal.user_requested_write=true only when the user explicitly asks to
  change, fix, implement, edit, write, apply, or continue an approved
  implementation.
```

Examples:

```json
{
  "intent": "coding",
  "goal": {
    "type": "review",
    "target": "current diff",
    "expected_result": "review findings",
    "user_requested_write": false,
    "needs_confirmation": false
  },
  "workflow_start": "review",
  "workflow_end": "review"
}
```

```json
{
  "intent": "coding",
  "goal": {
    "type": "review",
    "target": "current diff",
    "expected_result": "review findings fixed and verified",
    "user_requested_write": true,
    "needs_confirmation": false
  },
  "workflow_start": "review",
  "workflow_end": "verify"
}
```

## Runtime Behavior

### Turn Initialization

Before the first main-agent LLM call:

1. Resolve `GoalResolution`.
2. Update `TaskState.current_goal`.
3. Normalize route fields.
4. Activate `workflow_start` if it is known and not already active.
5. Store the route boundary in runtime state so later auto-advance decisions can
   read it.

The route boundary should be persisted with task state, not hidden in prompt text
only. A small structure is enough:

```python
class WorkflowRoute(BaseModel):
    start: str = ""
    end: str = ""
```

Add to `TaskState`:

```python
workflow_route: WorkflowRoute | None = None
```

### Auto-Advance Boundary Check

When an auto event is detected, runtime evaluates it against the route:

```text
current workflow = event.workflow
candidate target = DAG edge target for event.condition
route end = task_state.workflow_route.end
```

Rules:

- If there is no route end, preserve existing behavior.
- If `current workflow == route end`, mark the current workflow satisfied as
  appropriate, but set `should_continue=False`.
- If the candidate target is outside the allowed path to `route end`, stop and
  ask for user guidance.
- If the candidate target is on the allowed path to `route end`, activate it and
  continue.

For the review cases:

| Route | Review returns issues | Runtime behavior |
|---|---|---|
| `review -> review` | `review_has_issues` | Stop after review output; do not enter feedback. |
| `review -> verify` | `review_has_issues` | Enter `feedback`, then allow `feedback -> tdd -> verify`. |

### Manual `advance_workflow`

Manual `advance_workflow` calls should also respect the route boundary:

- advancing past `workflow_end` should be rejected or converted to a user-facing
  confirmation request;
- terminal `done` remains valid when the active workflow is at `workflow_end`;
- route override events should record evidence that they were constrained by the
  resolver route.

## Reconciliation

Replace direct `next_workflow` reconciliation with start-node reconciliation.

Initial logic:

```text
if workflow_start is set:
    if workflow_start already active:
        no-op
    elif active stale precursor can be superseded:
        mark precursor satisfied with superseded_by_intent
        activate workflow_start
    elif direct DAG edge exists from an active workflow:
        advance via that edge
    elif no active workflow conflicts:
        activate workflow_start as resolver-selected entry
```

This keeps the existing intent override behavior, but makes the field name match
the behavior.

## Implementation Plan

1. Add `workflow_start`, `workflow_end`, and optional `workflow_route`.
2. Update resolver prompt and tests to require route fields.
3. Update reconcile and turn initialization to consume `workflow_start`.
4. Update auto-advance and manual `advance_workflow` to check `workflow_end`.
5. Remove `next_workflow` from schema, prompt, normalization, and tests.

## Files To Change

| File | Change |
|---|---|
| `src/voidx/runtime/task_state.py` | Add route fields or `WorkflowRoute`; update `TaskState.update_after_turn`. |
| `src/voidx/agent/goal_resolver.py` | Update schema normalization and prompt guidance. |
| `src/voidx/workflow/reconcile.py` | Replace `next_workflow` routing with `workflow_start` routing. |
| `src/voidx/workflow/auto_advance.py` | Keep event detection pure; do not embed route policy here. |
| `src/voidx/agent/graph/tool_executor.py` | Apply route-boundary policy when auto-advance events update state. |
| `src/voidx/agent/runtime_context.py` | Render workflow route in Current Task State for observability. |
| `tests/test_agent/test_goal_resolver.py` | Add resolver schema and prompt coverage. |
| `tests/test_workflow_reconcile.py` | Add start-node reconciliation coverage. |
| `tests/test_agent/test_core_flow.py` | Add end-to-end review-only and review-and-fix route coverage. |

## Test Plan

### Goal Resolver

- "review this" returns `goal.type=review`, `user_requested_write=false`,
  `workflow_start=review`, `workflow_end=review`.
- "review this and fix issues" returns `goal.type=review`,
  `user_requested_write=true`, `workflow_start=review`, `workflow_end=verify`.
- "look at this and suggest fixes" does not set write intent and ends at review
  or inspect, not verify.
- Unknown workflow route names normalize to `None`.
- `next_workflow` is absent from the resolver schema and ignored by runtime
  route selection.

### Turn Initialization

- A new review-only request activates `review` before the first main LLM call.
- A new review-and-fix request activates `review` and stores route end `verify`.
- Existing active stale `brainstorm` can be superseded when
  `workflow_start=tdd` and write intent is explicit.

### Auto-Advance

- Review-only route plus `review_has_issues` sets `should_continue=False` and
  does not proceed into implementation.
- Review-and-fix route plus `review_has_issues` activates `feedback` and keeps
  `should_continue=True`.
- Review-and-fix route can proceed through `feedback -> tdd -> verify`.
- Automatic progression stops when `verify` reaches terminal completion.
- Existing `tdd -> verify` behavior remains unchanged when no route end is set.

### Regression

- Duplicate same-batch `read` calls remain deduplicated.
- Workflow gates still ask through permission policy rather than silently
  denying allowed user-approved actions.
- Plan mode still prevents implementation writes regardless of route fields.

## Risks

| Risk | Mitigation |
|---|---|
| Resolver sets broad `workflow_end=verify` too often | Prompt requires explicit fix/apply/write wording; tests cover suggest-only language. |
| Route state conflicts with active workflow state | Reconcile validates DAG paths and records superseded evidence. |
| Older resolver output includes `next_workflow` only | Ignore it and fall back to goal-derived route defaults. |
| Auto-advance policy leaks into pure detection | Keep `auto_advance.py` event-only; enforce route boundaries in graph/runtime state update. |

## Acceptance Criteria

- "review 一下这个" starts `review`, ends at `review`, and does not begin
  feedback/TDD without a later user approval.
- "review 完并修复问题" starts `review`, allows review findings to route into
  `feedback`, continues into `tdd`, and stops after verification.
- `Goal` remains available for UI/status, permissions, target scope, session
  restoration, and non-workflow goals.
- `next_workflow` is not part of the resolver contract.
- Focused workflow, goal resolver, and graph core tests pass.
