> **Status: Done** — Archived on 2026-08-01.

---
name: shared-control-protocol
display_name: Shared Control Protocol
description: Unify turn, chat, loop, and goal lifecycle control behind a shared event-driven control protocol abstraction while preserving each mode's lifecycle semantics.
doc_type: tech-design
audience: human+llm
status: draft
---

# Shared Control Protocol

## Summary

Runtime control should be modeled as one shared control event pipeline with mode-specific protocol adapters. Today coding/chat, loop, and goal all use model-facing lifecycle tools, but the runtime still contains protocol-specific branches for controller selection, prompt repair, terminal handling, and post-commit cleanup. This design introduces `ControlProtocol`, `ControlPolicy`, and explicit `ControlEvent` / `ControlAction` types so `llm_turn.py` can route control behavior uniformly while each mode keeps its own lifecycle semantics.

The design intentionally does **not** merge `turn`, `loop`, and `goal` into one model-facing tool in the first migration. Tool names and schemas remain stable. The abstraction is internal: shared control mechanics move into one pipeline, while protocol adapters own the differences between `turn(start/stop)`, `loop(commit)`, `goal(init)`, and `goal(decision)`.

## Problem

The pre-migration implementation had the start of a lifecycle protocol registry, but the abstraction was too narrow:

- The old protocol interface only exposed `tool_definitions`, `classify`, `decision_missing`, and `repair_prompt`.
- `src/voidx/agent/infrastructure/langgraph/runtime/llm_turn.py` still selects the controller with protocol-specific logic: goal gets `goal_controller`, all other non-turn protocols get `loop_controller`.
- `src/voidx/agent/infrastructure/langgraph/runtime/core/turn.py` hard-codes control prompts such as `TURN_START_PROMPT`, `TURN_STOP_PROMPT`, and `NO_USER_RESPONSE_PROMPT`.
- Loop and goal decision prompts are represented as a single `repair_prompt`, which does not cover other repair cases like missing initial declaration, invalid control calls, or visible-response requirements.
- Loop-specific cleanup (`strip_tool_calls_after_loop_commit`) lives next to protocol registration instead of as a protocol post-processing hook.

This makes new modes costly to add and caused real coupling mistakes: the graph runtime has to know whether a protocol is goal or loop before it can pass the right controller.

## Goals

- Provide one shared control pipeline for coding, chat, loop, and goal.
- Preserve existing model-facing tools and schemas in the first migration.
- Move controller selection behind the active protocol.
- Move prompt repair behavior behind event-specific control policies.
- Represent lifecycle decisions as explicit control events instead of scattered conditionals.
- Keep mode-specific semantics isolated in protocol adapters.
- Make future modes addable without editing `llm_turn.py` control branches.

## Non-Goals

- Do not replace `turn`, `loop`, and `goal` with a single universal model-facing tool yet.
- Do not change durable loop scheduling, goal attempt dispatch, or wakeup semantics.
- Do not change goal intake/evaluator tool schemas beyond what the active goal protocol already requires.
- Do not change chat/coding prompt content except where mechanically moved into protocol policy classes.
- Do not introduce a second runtime state machine parallel to the existing LangGraph loop.

## Current Modes

### Coding

Coding uses the default `RuntimeProfile(protocol="turn")`. The model receives the graph-owned `turn` tool. A valid turn starts with `turn(operation="start", params={intent, goal})`, then eventually commits a visible final answer with `turn(operation="stop", params=null)`. `turn(start)` updates `TaskState`, reconciles workflow runs, and rerenders the task context.

Current special behavior:

- Short initial plain text can trigger `TURN_START_PROMPT`.
- Long initial plain text may auto-commit for non-plan, non-goal interaction modes.
- Final plain text without `turn(stop)` can trigger `TURN_STOP_PROMPT`.
- `turn(stop)` without visible user text can trigger `NO_USER_RESPONSE_PROMPT`.

### Chat

Chat currently also uses the default `turn` protocol because `CHAT_PROFILE` does not override `RuntimeProfile.protocol`. It differs mainly through `ChatPromptPolicy` and `ChatToolView`, not through lifecycle protocol. The shared abstraction should allow chat to continue reusing turn behavior, but it should make chat-specific looseness explicit if needed later, for example allowing plain text completion without requiring the full start/stop barrier.

### Loop

Loop uses `RuntimeProfile(protocol="loop")`. The model receives the runtime-backed `loop` tool. The loop iteration should call `loop(operation="commit", outcome="continue", summary=...)` before the turn can end. The user, not the model, stops a running loop.

Current special behavior:

- The loop tool can accept `operation="start"`, but scheduled iteration prompts already tell the model to submit exactly one `commit` decision.
- Plain text, turn-like terminal messages, or mixed text+tools can become barriers if no loop decision has been submitted.
- Missing decisions trigger `LOOP_DECISION_PROMPT` up to a bounded repair count.
- After commit, dangling tool calls must be stripped so the turn can finish and the next wakeup can be scheduled.

### Goal

Goal uses `RuntimeProfile(protocol="goal")`, but phase matters:

- `goal_phase="intake"`: the model may clarify, then must call `goal(op="init")` to submit `GoalSpec` through `goal_intake_controller`.
- `goal_phase="work"`: the agent performs normal work toward the active goal using the work tool view.
- `goal_phase="evaluator"`: the model may use policy-approved verification tools, then must call `goal(op="decision")` through `goal_controller`.

Current special behavior:

- Intake and evaluator use the same `goal` tool but different required operations.
- Goal evaluator decision repair currently appears as `GoalProtocol.repair_prompt()`.
- Goal phase and controller routing live in `TurnExecutionContext` and tool execution context.

## Design

### Core Idea

`llm_turn.py` should stop asking “is this goal or loop?” and instead ask the active control protocol to handle control concerns. The runtime loop should perform these protocol-independent steps:

1. Resolve the active `ControlProtocol` from `RuntimeProfile.protocol` and `TurnExecutionContext`.
2. Ask the protocol for model-facing control tool definitions.
3. Run the LLM.
4. Classify the assistant message through the protocol.
5. Convert message + loop state + turn state into a `ControlEvent`.
6. Ask the protocol policy for a `ControlAction`.
7. Apply the action uniformly: retry with guidance, execute regular tools, commit terminal message, fail, or continue.
8. Ask the protocol to post-process committed messages if needed.

### Data Types

```python
@dataclass(frozen=True)
class ControlContext:
    runtime_profile: RuntimeProfile | None
    turn_context: TurnExecutionContext | None
    interaction_mode: str
    turn_state: str
    loop_state: LlmLoopState
    runtime_task_state: TaskState
    state_messages: list[BaseMessage]
    tool_definitions: list[dict[str, Any]]

class ControlEventType(str, Enum):
    INITIAL_DECLARATION_MISSING = "initial_declaration_missing"
    TERMINAL_COMMIT_MISSING = "terminal_commit_missing"
    VISIBLE_RESPONSE_MISSING = "visible_response_missing"
    INVALID_CONTROL_CALL = "invalid_control_call"
    VALID_CONTROL_DECLARATION = "valid_control_declaration"
    VALID_CONTROL_COMMIT = "valid_control_commit"
    REGULAR_TOOLS = "regular_tools"
    PLAIN_TEXT = "plain_text"
    POST_COMMIT_TOOL_CALLS = "post_commit_tool_calls"

@dataclass(frozen=True)
class ControlEvent:
    type: ControlEventType
    assistant_msg: AIMessage
    classification: ControlClassification
    has_text: bool
    text: str

class ControlActionType(str, Enum):
    RETRY_WITH_GUIDANCE = "retry_with_guidance"
    RUN_TOOLS = "run_tools"
    COMMIT_TERMINAL = "commit_terminal"
    HANDLE_DECLARATION = "handle_declaration"
    HANDLE_COMMIT = "handle_commit"
    FAIL = "fail"
    IGNORE = "ignore"

@dataclass(frozen=True)
class ControlAction:
    type: ControlActionType
    prompt: str = ""
    metric: str = ""
    terminal_msg: AIMessage | None = None
    terminal_visible: bool = True
```

The exact names can change during implementation, but the important boundary is stable: event detection and action application are shared; mode-specific interpretation belongs to protocol/policy classes.

### Protocol Interface

```python
class ControlProtocol(Protocol):
    protocol_id: str

    def tool_definitions(self, ctx: ControlContext) -> list[dict[str, Any]]: ...

    def controller(self, ctx: ControlContext) -> object | None: ...

    def classify(self, msg: AIMessage, ctx: ControlContext) -> ControlClassification: ...

    def detect_event(self, msg: AIMessage, ctx: ControlContext) -> ControlEvent: ...

    def action_for(self, event: ControlEvent, ctx: ControlContext) -> ControlAction: ...

    def post_commit_message(self, msg: AIMessage, ctx: ControlContext) -> AIMessage: ...
```

A smaller first implementation can keep event detection partly in `core/turn.py`, but the target shape should put event interpretation behind the protocol.

### Prompt Policy

Prompts should be event-specific, not represented as one `repair_prompt()` method.

```python
class ControlPromptEvent(str, Enum):
    INITIAL_DECLARATION_MISSING = "initial_declaration_missing"
    TERMINAL_COMMIT_MISSING = "terminal_commit_missing"
    VISIBLE_RESPONSE_MISSING = "visible_response_missing"
    INVALID_CONTROL_CALL = "invalid_control_call"

class ControlPromptPolicy(Protocol):
    def prompt_for(self, event: ControlPromptEvent, ctx: ControlContext) -> str | None: ...
```

Mapping examples:

- Turn protocol:
  - `INITIAL_DECLARATION_MISSING` -> `TURN_START_PROMPT`
  - `TERMINAL_COMMIT_MISSING` -> `TURN_STOP_PROMPT`
  - `VISIBLE_RESPONSE_MISSING` -> `NO_USER_RESPONSE_PROMPT`
- Loop protocol:
  - `TERMINAL_COMMIT_MISSING` -> loop decision prompt
  - `POST_COMMIT_TOOL_CALLS` -> no prompt; strip tool calls
- Goal protocol:
  - intake `TERMINAL_COMMIT_MISSING` -> prompt for `goal(op="init")`
  - evaluator `TERMINAL_COMMIT_MISSING` -> prompt for `goal(op="decision")`
  - work phase -> no forced lifecycle decision unless a future goal-work protocol requires it

### Protocol Implementations

#### `TurnControlProtocol`

Responsibilities:

- Inject `TURN_TOOL_DEFINITION`.
- Classify only `turn` tool calls as control calls.
- Handle `turn(start)` by updating `TaskState`, reconciling workflows, rerendering task context, and returning a retry action with the current `ToolMessage` guidance.
- Handle `turn(stop)` by validating there is a visible pending assistant message.
- Own turn-specific prompt selection.

This preserves the existing behavior in `core/turn.py`, but relocates mode-specific prompts and start/stop semantics out of the shared runtime loop.

#### `ChatControlProtocol`

Initial migration can alias this to `TurnControlProtocol` because chat currently uses the default protocol. A future refinement can subclass or configure it:

- allow plain text completion earlier,
- suppress `turn(start)` requirement,
- keep `turn(stop)` only when tool execution happened.

The abstraction should make this a protocol/policy choice, not a branch in `llm_turn.py`.

#### `LoopControlProtocol`

Responsibilities:

- Inject the `loop` tool schema.
- Resolve `ctx.turn_context.loop_controller`.
- Treat terminal barriers as missing decision when `controller.final_decision()` is absent.
- Prompt for `loop(operation="commit", outcome="continue", summary=...)` with a bounded repair count.
- Strip tool calls after a decision has committed.

Loop should not inherit turn stop semantics. It ends an iteration by recording a loop decision, not by producing a final answer and calling `turn(stop)`.

#### `GoalControlProtocol`

Responsibilities:

- Inject the `goal` tool schema and optional verification tool definitions.
- Resolve controller by phase:
  - intake -> `ctx.turn_context.goal_intake_controller`
  - evaluator -> `ctx.turn_context.goal_controller`
  - work -> `None` unless future work-phase control is introduced
- Require `goal(op="init")` in intake before intake can be considered successful.
- Require `goal(op="decision")` in evaluator before evaluator can end.
- Allow normal work behavior in work phase without forcing a lifecycle decision.

Goal phase handling must stay in the goal protocol because the same model-facing tool has different valid operations by phase.

## Event Handling Rules

### Plain Text

Plain text is not universally valid or invalid. The protocol decides:

- turn/coding: short initial plain text can trigger start prompt; longer plain text may auto-commit depending on interaction mode.
- chat: may allow direct completion with fewer repairs.
- loop: plain text near a terminal boundary is missing a loop decision.
- goal intake/evaluator: plain text is missing a required goal operation if no controller submission exists.
- goal work: plain text may be normal output, depending on the surrounding goal runner step.

### Regular Tools

Regular tool calls should remain executable unless the protocol says they cross a lifecycle barrier. For loop and goal evaluator, text plus regular tools can be a barrier because the model may believe it has finished but the lifecycle decision has not been submitted. Tool-only calls should usually continue.

### Invalid Control Calls

Invalid control calls should produce protocol-specific guidance, but the retry mechanics should be shared. The current `invalid_turn_repaired` flag can become a generic `invalid_control_repaired` flag or remain in `LlmLoopState` with a compatibility alias.

### Repair Limits

The current loop/goal decision repair cap should become a generic `max_protocol_repairs` policy value. The first migration can keep using `loop.protocol_repairs` to avoid broad state changes.

### Metrics

Existing metric names can be preserved initially for continuity. New generic metrics can be added after behavior is stable:

- `control_prompted`
- `control_prompt_succeeded`
- `control_invalid`
- `control_auto_committed`
- `control_decision_missing`

During migration, emit both old and new metrics only if needed by downstream consumers.

## File Structure

Proposed implementation files:

- `src/voidx/agent/infrastructure/langgraph/runtime/control_protocol.py`
  - Shared protocol interfaces, event/action dataclasses, registry.
- `src/voidx/agent/infrastructure/langgraph/runtime/control_events.py`
  - Optional split if event detection grows large.
- `src/voidx/agent/infrastructure/langgraph/runtime/control_actions.py`
  - Optional split for shared action application helpers.
- `src/voidx/agent/infrastructure/langgraph/runtime/protocols/turn.py`
  - Turn/coding implementation.
- `src/voidx/agent/infrastructure/langgraph/runtime/protocols/chat.py`
  - Optional chat policy; can initially alias turn.
- `src/voidx/agent/infrastructure/langgraph/runtime/protocols/loop.py`
  - Loop implementation.
- `src/voidx/agent/infrastructure/langgraph/runtime/protocols/goal.py`
  - Goal implementation.

Compatibility path:

- Migrate callers directly to `control_protocol.py`; delete the old graph-protocol entry point once internal imports and tests are moved.
- Keep `TurnClassification` and `classify_turn_call` during the first migration; rename only after behavior is stable.

## Migration Plan

### Phase 1: Introduce Shared Types Without Behavior Changes

- Add `ControlContext`, `ControlEvent`, `ControlAction`, and `ControlProtocol` types.
- Add `resolve_control_protocol(profile)` as the primary registry entry point.
- Migrate old protocol imports directly to `ControlProtocol` instead of keeping an alias.
- Add unit tests that resolving `coding`, `chat`, `loop`, and `goal` returns the expected protocol id.

Verification:

```bash
./test.py --backend -- src/tests/test_agent/goal/test_goal_protocol.py src/tests/test_agent/graph/test_loop_protocol_injection.py
```

### Phase 2: Move Controller Resolution Into Protocols

- Replace `goal_controller if protocol_id == "goal" else loop_controller` in `llm_turn.py` with `control_protocol.controller(control_context)`.
- Add tests for goal intake, goal evaluator, loop, and turn contexts to ensure the selected controller is correct.
- This phase fixes the most concrete current coupling bug with minimal behavior change.

Verification:

```bash
./test.py --backend -- src/tests/test_agent/goal/test_goal_protocol.py src/tests/test_agent/test_goal_intake.py src/tests/test_agent/graph/test_run_loop_startup.py
```

### Phase 3: Replace `repair_prompt()` With Event Prompts

- Add `prompt_for(event, ctx)` or `action_for(event, ctx)` to protocol classes.
- Move `TURN_START_PROMPT`, `TURN_STOP_PROMPT`, `NO_USER_RESPONSE_PROMPT`, loop decision prompt, and goal decision/init prompts behind protocol policies.
- Keep the same prompt strings initially.
- Update tests to assert behavior, not exact helper names.

Verification:

```bash
./test.py --backend -- src/tests/test_agent/test_mode_dispatch.py src/tests/test_agent/goal/test_goal_protocol.py src/tests/test_agent/graph/test_loop_protocol_injection.py
```

### Phase 4: Centralize Event Detection and Action Application

- Convert branches in `handle_turn_control_response` into event detection + action application.
- Keep start/stop implementation details as protocol handlers until they are safely decomposed.
- Preserve existing auto-commit behavior for non-plan/non-goal interaction modes.

Verification:

```bash
./test.py --backend -- src/tests/test_agent src/tests/test_agent/graph
```

### Phase 5: Optional Chat-Specific Policy

- Introduce `ChatControlProtocol` only if product behavior needs chat to be less strict than coding.
- If chat should stay identical to coding, keep `CHAT_PROFILE.protocol` unset/default and do not add extra code.

Verification:

```bash
./test.py --backend -- src/tests/test_agent/slash/test_slash_session.py src/tests/test_agent/test_mode_dispatch.py
```

## Testing Strategy

Required targeted coverage:

- Turn start prompt still triggers for short initial plain text in coding mode.
- Turn stop prompt still triggers for final text without `turn(stop)`.
- No-user-response prompt still triggers when stop/invalid control happens before visible text.
- Loop iteration cannot end without `loop(commit)` when a terminal barrier is reached.
- Loop commit still strips post-commit dangling tool calls.
- Goal intake succeeds only through `goal(op="init")`.
- Goal evaluator succeeds only through `goal(op="decision")`.
- Goal work phase does not accidentally require evaluator decisions.
- Chat behavior remains unchanged unless an explicit chat policy is introduced.

Regression commands:

```bash
./test.py --backend -- \
  src/tests/test_agent/goal/test_goal_domain.py \
  src/tests/test_agent/goal/test_goal_protocol.py \
  src/tests/test_agent/graph/test_loop_protocol_injection.py \
  src/tests/test_agent/graph/test_run_loop_startup.py \
  src/tests/test_agent/slash/test_slash_session.py \
  src/tests/test_agent/test_goal_intake.py \
  src/tests/test_agent/test_mode_dispatch.py
```

Run the broader backend suite after the event/action refactor phase:

```bash
./test.py --backend
```

## Risks

- **Over-abstraction:** A universal control layer can become vague if it hides lifecycle semantics. Mitigation: keep protocol classes explicit and keep tool schemas unchanged.
- **Prompt drift:** Moving prompt strings may alter repair behavior. Mitigation: migrate prompts verbatim first, then refine in separate changes.
- **Auto-commit regression:** Coding/chat plain-text auto-commit behavior is subtle. Mitigation: write focused tests before moving `_handle_plain_text` logic.
- **Goal phase confusion:** Goal uses one tool for two phases. Mitigation: protocol controller resolution and prompt selection must key off `goal_phase`.
- **Loop scheduling regression:** Executing tool calls after loop commit can prevent wakeup scheduling. Mitigation: preserve post-commit strip behavior as a protocol hook before deeper refactor.

## Open Questions

- Should chat eventually have its own `protocol="chat"`, or should it remain a prompt/tool-policy variant of `turn`?
- Should `LlmLoopState` be renamed to a generic control loop state, or should the first migration keep the name for compatibility?
- Should protocol-specific metrics be preserved indefinitely, or should they emit generic control metrics after migration?
- Should malformed tool-call repair remain outside control protocol, since it is provider-level rather than lifecycle-level?

## Definition of Done

- `llm_turn.py` no longer contains protocol-id branches for selecting loop vs goal controllers.
- Control prompt selection is event-based and owned by protocol/policy classes.
- `turn`, `loop`, and `goal` model-facing schemas remain stable.
- Existing coding/chat/loop/goal lifecycle tests pass.
- Goal intake and evaluator no longer depend on final assistant text as lifecycle transport.
- Adding a new lifecycle mode requires registering a protocol adapter rather than modifying shared turn-loop branches.
