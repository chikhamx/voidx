# Agent Tool Mode Contract

> **Date**: 2026-06-16
> **Status**: Spec

## Context

`agent` tool delegation currently exposes internal workflow details to the main model. The caller must provide `goal_resolution` and `result`, including workflow `plan.join` / `plan.leave` and a free-form result format. This is precise for runtime execution, but it is too easy for the model to omit fields, choose mismatched workflow nodes, or over-broaden a child task.

The old delegation contract also relied on free-text fields such as `expected_output` and `parent_evidence`, with review-specific keyword checks. That made review delegation brittle: valid review requests could fail because the text did not contain the expected markers.

The public `agent` schema should expose a smaller, task-oriented contract. Runtime code should derive workflow routing and result contracts from that public contract.

## Goals

- Make child-agent delegation easy for the model to call correctly.
- Keep one child agent focused on one bounded target.
- Preserve structured workflow routing through `GoalResolution`.
- Preserve structured child output through `AgentResultContract`.
- Replace free-text keyword validation with enum-driven validation.
- Surface actionable delegation errors to the caller.

## Non-Goals

- Do not remove the workflow runtime.
- Do not remove `GoalResolution` or `AgentResultContract` internally.
- Do not add multi-target child-agent work in this change.
- Do not change child-agent execution semantics beyond input normalization.

## Public Schema

`AgentInput` should expose these model-facing fields:

```python
class AgentInput(BaseModel):
    agent: str = "voidx"
    mode: Literal["inspect", "review", "debug", "plan", "implement", "feedback"]
    task: str
    target: str
    success_criteria: str = ""
    result_preset: Literal[
        "auto",
        "inspection",
        "review",
        "debug",
        "plan",
        "implementation",
        "feedback",
    ] = "auto"
    model: str | None = None
```

Field semantics:

- `mode`: The kind of child work requested. This drives workflow routing.
- `task`: Complete, self-contained task brief for the child agent.
- `target`: A single file, module, directory, behavior, or issue scope.
- `success_criteria`: What counts as done. Required for write-oriented modes.
- `result_preset`: Short enum that selects an internal result contract.

`target` is intentionally singular. If the parent wants multiple independent reviews or inspections, it should issue multiple `agent` calls, one target per child.

## Mode Routing

`mode` determines the internal `GoalResolution.plan.join` and `plan.leave`.

| mode | goal.type | plan.join | plan.leave | Notes |
|------|-----------|-----------|------------|-------|
| `inspect` | `inspect` | `review` | `review` | Temporary mapping until an `inspect` workflow node exists. Must not require review verdict semantics unless preset resolves to review. |
| `review` | `review` | `review` | `review` | Focused code review of one target. |
| `debug` | `debug` | `debug` | `debug` | Root-cause investigation only. |
| `plan` | `design` | `plan` | `plan` | Produce an implementation plan or design analysis. |
| `implement` | `feature` | `tdd` | `verify` | Write-capable child work. Requires `success_criteria`. |
| `feedback` | `review` | `feedback` | `verify` | Act on review feedback. Requires `success_criteria`. |

The normalization layer should build:

```python
GoalResolution(
    intent=IntentResolution(type=TaskIntent.CODING, desc=task),
    goal=GoalSpec(type=goal_type, desc=f"{mode}: {target}"),
    plan=PlanResolution(join=join, leave=leave),
)
```

The existing `_subagent_step_budget()` can continue to derive step budget from `plan.join`.

## Result Presets

`result_preset` is not a user-provided schema. It is an enum used by `AgentTool` to generate the internal `AgentResultContract`.

`auto` should select a preset from `mode`:

| mode | auto preset |
|------|-------------|
| `inspect` | `inspection` |
| `review` | `review` |
| `debug` | `debug` |
| `plan` | `plan` |
| `implement` | `implementation` |
| `feedback` | `feedback` |

Preset contracts:

| preset | schema_name | format |
|--------|-------------|--------|
| `inspection` | `inspection_result` | `summary, evidence, findings, open_questions` |
| `review` | `review_result` | `verdict=PASS|FAIL|NEEDS_CHANGE, findings, risks, next_actions` |
| `debug` | `debug_result` | `root_cause, evidence, reproduction, fix_direction, open_questions` |
| `plan` | `plan_result` | `plan_summary, tasks, files, tests, risks` |
| `implementation` | `implementation_result` | `status, files_changed, tests_run, risks, followups` |
| `feedback` | `feedback_result` | `feedback_status, accepted, rejected, changes_needed, verification_notes` |

## Validation

Validation should be explicit and field-based:

- `task` must be at least 12 non-whitespace characters.
- `target` is required for all modes.
- `review` must have one concrete `target`.
- `implement` requires non-empty `success_criteria`.
- `feedback` requires non-empty `success_criteria`.
- `result_preset` must be compatible with `mode`, unless it is `auto`.

Compatibility rules:

| mode | allowed explicit presets |
|------|--------------------------|
| `inspect` | `inspection`, `review` |
| `review` | `review` |
| `debug` | `debug`, `inspection` |
| `plan` | `plan`, `inspection` |
| `implement` | `implementation` |
| `feedback` | `feedback`, `implementation` |

Rejected calls should return a `ToolResult` with:

- `metadata.error=True`
- `metadata.validation_error=True` or `metadata.delegation_rejected=True`
- output that names the exact missing or incompatible field

Example:

```text
Child agent delegation rejected. mode='review' requires target.
```

## Description Construction

The child-agent prompt payload should be generated from normalized fields:

```text
Task: <task>
Mode: <mode>
Target: <target>
Success criteria: <success_criteria or "Report concrete findings and blockers.">

Result contract:
- schema_name: <schema_name>
- format: <format>
Return the final answer using this contract.
```

This keeps the public schema simple while still giving the child run the same structured contract it receives today.

## UI/Error Handling

When the child runner raises, the existing `AgentTool` returns:

```text
Child agent '<name>' failed: <exception>
```

The UI currently marks the subagent node as failed but can hide the actionable failure detail. The implementation should ensure the tool result remains visible enough for the parent model to recover, and ideally show the failure reason in the failed subagent node body.

## Testing

Focused tests should cover:

- `AgentTool.parameters_schema()` exposes `mode`, `task`, `target`, `success_criteria`, and `result_preset`.
- Schema no longer requires model-facing `goal_resolution` or `result`.
- `mode=review` normalizes to `goal.type=review`, `plan.join=review`, `plan.leave=review`.
- `mode=implement` normalizes to `goal.type=feature`, `plan.join=tdd`, `plan.leave=verify`.
- `mode=inspect` normalizes to `goal.type=inspect`, `plan.join=review`, `plan.leave=review`.
- `result_preset=auto` picks the expected contract for each mode.
- Review rejects missing `target`.
- Implement and feedback reject missing `success_criteria`.
- Invalid preset/mode combinations are rejected with actionable output.
- `_subagent_runner` still receives `GoalResolution` and `AgentResultContract`.

## Migration

Implementation can keep a private helper such as:

```python
def normalize_agent_input(inp: AgentInput) -> NormalizedAgentDelegation:
    ...
```

`NormalizedAgentDelegation` should contain the current internal fields:

- `description`
- `goal_resolution`
- `result_contract`
- `model`

After this migration, the old public `goal_resolution` and `result` fields should be removed from the model-facing schema. If backward compatibility is required for one release, accept the old shape behind a private compatibility path but do not advertise it in `parameters_schema()`.
