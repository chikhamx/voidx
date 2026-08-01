> **Status: Done** — Archived on 2026-08-01.

---
name: goal-intake-init-protocol
display_name: Goal Intake Init Protocol
description: Replace final-message JSON parsing in goal intake with goal(op=init) and strict goal(op=decision) lifecycle tool calls.
doc_type: tech-design
audience: human+llm
status: draft
---

# Goal Intake Init Protocol

## Summary

Goal intake should stop parsing `GoalSpec` from `final_assistant_summary`. The intake LLM needs to clarify with the user interactively, then submit the completed spec through the existing `goal` tool using `op="init"`. Evaluator decisions should use the same `goal` tool with `op="decision"`. The migration is intentionally strict: legacy `goal({"status": ...})` calls are invalid and all prompts/tests must move to the explicit `op` protocol.

## Problem

`src/voidx/agent/application/goal_intake.py` currently asks the model to emit final JSON text, then parses `result.final_assistant_summary`. That is an unreliable transport for structured state because `final_assistant_summary` is derived from the last assistant message in `src/voidx/agent/infrastructure/langgraph/adapter.py`, not from a committed data channel.

This fails when intake is interactive:

1. The intake prompt asks for JSON or `clarify`.
2. The model calls `clarify` when the user's first message lacks an explicit acceptance condition.
3. After the user answers, the model may emit a valid JSON object.
4. Any later guidance, protocol repair, or free-text continuation can become the last assistant message.
5. `GoalIntakeService` parses the wrong message and raises `GoalIntakeError` even though a complete spec was produced earlier.

The design bug is using assistant text as a lifecycle boundary. Goal lifecycle state should move through tool arguments and controllers, not through rendered final prose.

## Goals

- Keep goal intake interactive: the model can call `clarify` until the goal is complete.
- Reuse the existing `goal` tool instead of adding a second goal-start tool.
- Introduce `goal(op="init")` as the only way to submit an intake `GoalSpec`.
- Introduce `goal(op="decision")` as the only way to submit evaluator lifecycle decisions.
- Remove `final_assistant_summary` JSON parsing from goal intake.
- Do not preserve compatibility with legacy `goal({"status": ...})` calls.

## Non-Goals

- Do not make `goal` tool directly call `GoalService.start(...)`.
- Do not replace interactive intake with one-shot structured output.
- Do not change durable goal attempt dispatch, outbox, or wakeup semantics.
- Do not rename `GoalService.start`; only the tool operation is named `init`.

## Protocol

### `goal(op="init")`

Allowed only in `goal_phase="intake"`.

```json
{
  "op": "init",
  "objective": "Run the user-requested recurring joke interaction",
  "acceptance_condition": "The assistant tells exactly 10 jokes, roughly one minute apart, unless interrupted by the user.",
  "achievement_method": "Use the conversation to deliver one joke immediately, then continue at about 60-second intervals.",
  "max_attempts": 20
}
```

Fields:

- `op`: required literal `"init"`.
- `objective`: required non-empty string.
- `acceptance_condition`: required non-empty string.
- `achievement_method`: optional string, defaults to empty.
- `max_attempts`: optional integer, default `20`, range `1..200`.

Execution:

- Validate fields into `voidx.agent.domain.goal.GoalSpec`.
- Submit the spec to an intake-scoped controller.
- Return tool metadata with `goal_init_submitted=true` and the serialized `goal_spec`.
- Do not start the goal thread from inside the tool.

### `goal(op="decision")`

Allowed only in `goal_phase="evaluator"`.

```json
{
  "op": "decision",
  "status": "continue",
  "summary": "First joke delivered; remaining nine jokes are pending.",
  "evidence": "Assistant output contains joke 1 of 10.",
  "next": "Deliver joke 2 after the next scheduled wakeup.",
  "reason": "partial_completion",
  "progress": "meaningful"
}
```

Fields:

- `op`: required literal `"decision"`.
- `status`: required enum `"finished" | "continue" | "blocked"`.
- `summary`: required by `GoalController.submit_decision(...)` semantics; empty summaries remain invalid.
- `evidence`: optional string for traceability.
- `next`: optional string for the next action hint.
- `reason`: optional stable reason/progress key.
- `progress`: enum `"none" | "partial" | "meaningful"`, default `"none"`.

Execution:

- Map `status="finished"` to runtime outcome `"completed"`.
- Map `status="continue"` to runtime outcome `"continue"`.
- Map `status="blocked"` to runtime outcome `"blocked"`.
- Submit through the existing `GoalController.submit_decision(...)` path.

### Invalid Calls

The tool must reject these without side effects:

- Missing `op`.
- `op="init"` outside intake.
- `op="decision"` outside evaluator.
- Legacy `{"status": ...}` without `op`.
- Empty `objective`, empty `acceptance_condition`, invalid `max_attempts`, invalid `status`, or empty evaluator `summary`.

## Architecture

### Intake Controller

Add a small controller for the intake phase, separate from evaluator `GoalController`.

Suggested file: `src/voidx/agent/goal/intake_controller.py`.

```python
class GoalIntakeController:
    def __init__(self) -> None:
        self._spec: GoalSpec | None = None

    async def submit_init(self, spec: GoalSpec) -> GoalSpec:
        if self._spec is None:
            self._spec = spec
        return self._spec

    def final_spec(self) -> GoalSpec | None:
        return self._spec
```

The controller is attempt-local to the intake turn. It prevents duplicate `init` calls from changing the selected spec.

### Context Plumbing

Update both runtime and tool contexts:

- `src/voidx/agent/domain/turn_context.py`
  - Add `goal_intake_controller: Any | None = None`.
- `src/voidx/tools/base.py`
  - Add `goal_intake_controller: Any | None = Field(default=None, exclude=True)`.
- Tool context construction must copy `goal_intake_controller` from `TurnExecutionContext` into `ToolContext`. Locate this in the LangGraph tool execution context builder before implementation.

### Goal Tool

Update `src/voidx/tools/goal.py`:

- Replace `GoalDecisionInput` as the top-level schema with a strict `op` protocol.
- Implement a discriminated parse between `GoalInitInput` and `GoalDecisionInput`.
- Do not fallback from missing `op` to decision mode.
- Keep one tool id: `GoalTool.id == "goal"`.

Schema shape should expose a required `op` enum. If Pydantic's generated discriminated union is not accepted by all providers, write the JSON schema manually with `oneOf` or with all fields plus conditional validation in `execute`. Runtime validation remains authoritative either way.

### Tool Visibility

Update `src/voidx/agent/domain/goal.py`:

- `phase="intake"` should allow `clarify` and `goal`.
- `phase="evaluator"` should allow `goal`.
- `phase="work"` should continue to exclude `goal`.

Update `src/voidx/agent/goal/runner.py` `_available_goal_tool_ids()` only if needed; it already includes `goal`.

### Intake Service

Update `src/voidx/agent/application/goal_intake.py`:

- Remove `_extract_json` and the final-message JSON requirement.
- Add a `GoalIntakeController` instance.
- Pass it through `TurnExecutionContext(goal_phase="intake", goal_intake_controller=controller)`.
- After `runtime.run_turn(...)`, read `controller.final_spec()`.
- If no spec was submitted, raise `GoalIntakeError` with a user-facing message that says intake did not receive `goal(op="init")`.
- If a spec exists, call `goal_service.start(parent_thread_id, spec)`.

The LLM final message is no longer part of the success path.

### Intake Prompt

Replace the final JSON instruction with tool protocol guidance:

```text
You are initializing an autonomous Goal.

Rules:
- If objective or acceptance_condition is unclear, call clarify with one targeted question.
- When both are clear, call goal with op="init" and the complete spec.
- Do not emit the spec as JSON text.
- Do not call goal(op="decision") during intake.
- You may read project files only when needed to ground the spec.

User request:
{user_input}
```

The first ambiguous message should drive `clarify`. The post-clarify response should drive `goal(op="init")`.

### Evaluator Prompt

Update `src/voidx/agent/goal/runner.py` `_evaluator_prompt(...)` from:

```text
then call goal with a final status.
```

to:

```text
then call goal with op="decision" and status="finished", "continue", or "blocked".
```

Update `src/voidx/agent/infrastructure/langgraph/runtime/control_protocol.py` `GoalProtocol.repair_prompt()` similarly.

## Data Flow

```text
User first message
  -> AgentService._route_autonomous_first_message(...)
  -> GoalIntakeService.run(...)
  -> runtime.run_turn(intake prompt, tools: clarify + goal)
       -> clarify(...) zero or more times
       -> goal(op="init", objective=..., acceptance_condition=...)
       -> GoalIntakeController.final_spec()
  -> goal_service.start(parent_thread_id, spec)
  -> GoalService creates goal thread/outbox
  -> GoalRuntimeRunner work phase
  -> GoalEvaluator evaluator phase
       -> goal(op="decision", status=...)
  -> dispatcher commits RuntimeDecision
```

## Migration Plan

1. Add `GoalIntakeController` and context fields.
2. Extend `GoalTool` to require `op="init" | "decision"` with no legacy compatibility.
3. Allow `goal` in `GoalToolView` intake phase.
4. Rewrite `GoalIntakeService` to use `controller.final_spec()` instead of `final_assistant_summary`.
5. Rewrite intake prompt to require `goal(op="init")`.
6. Rewrite evaluator prompt and `GoalProtocol.repair_prompt()` to require `goal(op="decision")`.
7. Update all tests and fixtures that call `goal` without `op`.
8. Remove `_extract_json` tests and add controller/tool-protocol tests.

## Test Plan

Focused backend tests:

```bash
./test.py --backend -- \
  src/tests/test_tools/test_goal_tool.py \
  src/tests/test_agent/test_goal_intake.py \
  src/tests/test_agent/test_mode_dispatch.py \
  src/tests/test_agent/goal/test_goal_domain.py \
  src/tests/test_agent/goal/test_goal_evaluator.py \
  src/tests/test_agent/goal/test_goal_runner.py \
  src/tests/test_agent/goal/test_goal_protocol.py \
  src/tests/test_agent/graph/test_loop_protocol_injection.py \
  -q
```

Add or update tests for:

- `GoalTool().parameters_schema()` exposes required `op`.
- Missing `op` is invalid.
- `goal(op="init")` submits only in intake phase.
- `goal(op="decision")` submits only in evaluator phase.
- `GoalToolView.default(phase="intake")` binds `clarify` and `goal`.
- `GoalIntakeService.run(...)` starts a goal from controller-submitted spec.
- `GoalIntakeService.run(...)` raises if no `goal(op="init")` was submitted.
- `GoalIntakeService` does not inspect `final_assistant_summary` for success.
- Existing evaluator runner commits `finished`, `continue`, and `blocked` decisions via `op="decision"`.

## Acceptance Criteria

- Goal intake success does not depend on assistant final text.
- Interactive clarification still works before goal initialization.
- The only successful intake submission path is `goal(op="init")`.
- The only successful evaluator decision path is `goal(op="decision")`.
- Legacy `goal({"status": ...})` is rejected.
- No `GoalSpec` JSON parsing remains in `GoalIntakeService`.
- Focused tests above pass with `./test.py --backend -- ... -q`.

## Open Questions

- Should duplicate `goal(op="init")` calls return the first spec as idempotent success, or return an explicit duplicate-submission error? The proposed design is idempotent first-write-wins.
- Should `goal(op="init")` require `achievement_method` when the user gave explicit timing or sequencing instructions, or keep it optional and let the model decide? The proposed design keeps it optional.
- Should `evidence` in `op="decision"` be persisted in `RuntimeDecision.metadata`, since the current `GoalController` only persists summary/progress/reason? This is outside the intake fix but worth addressing separately.
