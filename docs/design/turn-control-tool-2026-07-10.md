# Explicit Turn Completion Tool

## Status

Exploratory design.

## Problem

The current graph treats any assistant message without tool calls as a terminal
answer:

```text
tool calls present -> execute tools
no tool calls       -> end turn
```

This conflates two different model actions:

- a progress update such as "I have enough evidence; let me summarize"
- a complete user-facing answer

When the model emits a progress update without another tool call, the graph
ends normally even though the requested result was not delivered. Detecting
this from natural-language keywords or a generic completion judge is not
reliable across arbitrary user goals.

## Decision

Introduce a graph-owned control tool named `turn`.

The model calls `turn()` to commit the current user-facing response and
terminate the top-level turn. Ordinary assistant text without a tool call is
provisional rather than immediately terminal.

This is a two-phase completion protocol:

1. produce the candidate user-facing response as normal assistant text
2. commit the latest candidate response with `turn()`

`turn` carries no response or status data. It is only a barrier signal. The
runtime owns the pending assistant text and commits the latest non-empty
candidate when the barrier is received.

`turn` is a protocol signal, not a normal runtime tool:

- it is included in the main model's bound tool definitions
- it is intercepted inside the graph before tool authorization or execution
- it never creates a `ToolMessage`
- it is not registered in `ToolRegistry`
- it is not exposed to subagents in the first implementation

## Tool Schema

```json
{
  "type": "function",
  "function": {
    "name": "turn",
    "description": "Commit the latest assistant response and end the current user turn. Call only when the current response is ready to be delivered. Do not call with any other tool.",
    "strict": true,
    "parameters": {
      "type": "object",
      "properties": {},
      "required": [],
      "additionalProperties": false
    }
  }
}
```

Blocked work is represented in the assistant response itself before calling
`turn()`. Requests for interactive user input should continue to use the
existing clarification mechanism instead of calling `turn`.

## Model Contract

The runtime prompt should define these rules:

1. Ordinary assistant text is provisional until committed by `turn()`.
2. If the latest assistant response is complete, call `turn()` without
   repeating the response.
3. If the response is incomplete, continue working instead of calling `turn`.
4. `turn` must be the only tool call in its assistant message.
5. Do not call `turn` after a progress update or incomplete response.

## Graph Semantics

The existing graph topology can remain unchanged if `_call_llm` normalizes a
valid `turn` call into a terminal `AIMessage`.

```text
call_llm
  |
  +-- regular tool calls -----------------> execute_tools
  |
  +-- valid turn call
  |     commit latest pending assistant text
  |     create terminal AIMessage
  |     return without tool calls --------> finalize -> END
  |
  +-- plain assistant text
        retain as latest provisional candidate
        if missing-turn count <= 2:
          append progressively stronger guidance
          call model again within _call_llm
        else:
          commit latest candidate and end
  |
  +-- invalid turn call
        append repair guidance
        call model again within _call_llm
```

The normalized terminal message should retain provider response metadata and
usage metadata where possible, but it must not retain the `turn` tool call.
This allows the existing router to continue using:

```python
if last.tool_calls:
    return "execute"
return "end"
```

The difference is that `_call_llm` only returns a tool-free message after a
valid `turn` call or an explicit compatibility fallback.

## Validation Rules

A valid `turn` call must satisfy all of the following:

- exactly one tool call is present
- the tool name is `turn`
- the tool arguments are an empty object
- a non-empty provisional assistant response is pending
- no regular tool call appears in the same assistant message

The runtime does not attempt to determine whether the response is semantically
correct or sufficiently detailed. The tool solves structural ambiguity, not
general answer-quality evaluation.

## Repair Behavior

### Missing `turn`

If the model returns plain assistant text without any tool calls, keep that
message as the pending provisional response and inject hidden guidance:

First consecutive miss:

```text
Your previous assistant response has not been committed.
If it is complete, call turn() now as the only tool.
If it is incomplete, continue the task using the necessary tools.
```

Second consecutive miss:

```text
Final completion check: do not return another standalone assistant response.
Either call a regular tool to continue required work, or call turn() as the
only tool to commit the latest response and finish this turn.
```

Call the model after each prompt. A successful regular tool call resets the
consecutive missing-turn count because the agent has resumed material work.

If the model produces a third consecutive standalone assistant response, the
runtime commits the latest non-empty provisional response and ends the turn.
This bounded fallback prevents an infinite protocol loop and preserves
compatibility with models that do not reliably emit the barrier call.

### Invalid or mixed call

If `turn` is empty, malformed, or mixed with other tools, do not execute any
tool from that assistant message. Inject:

```text
The turn control call was invalid. Call regular tools first in a separate
assistant step. When all work is complete, call turn as the only tool and
provide the complete response.
```

Retry the model once.

## Streaming and UI

The candidate response is streamed as ordinary assistant text. It remains
visible while awaiting the barrier but is not persisted as the terminal
assistant message yet.

When `turn()` is received:

1. do not display the internal control call
2. commit the existing assistant stream
3. persist the latest provisional text as the terminal `AIMessage`

When the third-miss fallback is reached, perform the same commit without a
`turn()` call and record that the barrier was bypassed.

Progress-only assistant text may remain visible during the repair attempt, but
it must not be persisted as the terminal assistant message.

## Tool Execution Boundaries

`turn` must be intercepted before:

- repetitive-tool guards
- permission evaluation
- tool-start UI events
- workspace locking
- `ToolRegistry.execute_tool`
- tool-result persistence

Registering it as a normal `BaseTool` would incorrectly produce a
`ToolMessage`, route through permissions, and require another model call after
the terminal signal.

The control definition should live in a graph-owned module such as:

```text
src/voidx/agent/graph/turn_control.py
```

Suggested responsibilities:

- define the strict tool schema
- append the tool definition to main-agent tool definitions
- classify assistant messages as regular tools, valid turn, or protocol error
- normalize a valid call into a terminal `AIMessage`
- produce repair guidance

## Provider Compatibility

Explicit completion should initially be gated by model capability or a feature
flag.

Recommended rollout modes:

- `off`: current behavior; tool-free assistant text ends the turn
- `barrier`: advertise `turn`, perform up to two escalating completion checks,
  then commit the third standalone response as a bounded fallback

Start with `barrier` for supported tool-calling models and collect protocol
failure metrics.

Models without reliable function/tool calling must remain on `off`.

## Subagents

Do not expose `turn` to subagents initially.

Subagents already have a separate result contract and return a string to the
parent. Adding the same control signal to both loops would conflate top-level
user completion with child-agent completion. A later change may introduce a
separate child result control if needed.

## Telemetry

Record structured counters:

- `turn_control_called`
- `turn_control_missing`
- `turn_control_invalid`
- `turn_control_mixed_tools`
- `turn_control_first_prompt`
- `turn_control_second_prompt`
- `turn_control_prompt_succeeded`
- `turn_control_third_miss_fallback`

These counters are necessary to determine whether particular providers or
models can reliably use barrier mode.

## Implementation Areas

Expected implementation changes:

- `src/voidx/agent/graph/turn_control.py`
  - tool schema, parsing, validation, normalization, repair guidance
- `src/voidx/agent/graph/core/llm.py`
  - bind the control tool
  - handle valid and invalid completion calls
  - perform one bounded protocol repair
  - emit the final response stream
- `src/voidx/agent/prompts.py` or runtime context instructions
  - add concise completion protocol rules
- configuration models/settings
  - add `off` and `barrier` modes if rollout is configurable
- focused graph tests
  - cover completion, repair, invalid calls, and compatibility behavior

The normal tool registry, permission engine, and tool executor should not need
to change.

## Required Tests

1. A valid `turn()` commits the latest provisional response as one terminal
   `AIMessage`.
2. `turn()` without pending provisional text is rejected.
3. A regular tool call still routes to tool execution.
4. The first plain-text response triggers the first completion prompt.
5. A following `turn()` commits the existing response without duplicating it.
6. A second consecutive plain-text response triggers the stronger prompt.
7. A regular tool call resets the consecutive missing-turn count.
8. A third consecutive plain-text response is committed through the bounded
   fallback.
9. A mixed `turn` plus regular-tool message is rejected without executing the
   regular tool.
10. The control call never emits tool permission or tool execution events.
11. A successful barrier emits exactly one committed assistant stream.
12. The third-miss fallback emits exactly one committed assistant stream.
13. Subagent tool definitions do not contain `turn`.

## Non-Goals

- determining whether an arbitrary answer is factually correct
- determining whether every user requirement was semantically satisfied
- replacing workflow-specific verification or test gates
- forcing subagents to use the same completion protocol
- interpreting progress text through keyword matching

## Resolved Questions

1. **Progress-only text visibility after repair**: progress-only provisional text
   remains visible during the repair attempt. It is not collapsed into a
   transient status item. Rationale: collapsing would require UI-side state
   tracking of which streamed segments are provisional vs committed, adding
   complexity for marginal benefit. The text is simply not persisted as the
   terminal `AIMessage` if a later candidate supersedes it.

2. **Third-miss fallback commit strategy**: commit the latest non-empty
   candidate only. Do not merge consecutive provisionals. Rationale: the
   `messages` field uses LangGraph's `add_messages` reducer; merging would
   require `RemoveMessage` operations to delete earlier provisionals before
   appending the merged result, which is fragile. Committing latest-only is
   consistent with the existing `replacement_messages` pattern in `_call_llm`.

3. **Provider reliability configuration**: by protocol, not by individual model
   profile. Rationale: tool-calling reliability is primarily a protocol-level
   property (OpenAI/Anthropic tool-calling vs DeepSeek XML vs others). The
   existing code already branches on protocol in `strip_gemini_unsupported_schema_keys`
   and `resolve_protocol`. Per-model overrides can be added later if telemetry
   shows intra-protocol variance.

## Implementation Details

### Repair Guidance Injection Mechanism

Repair guidance uses the same in-loop pattern as `MALFORMED_TOOL_CALL_REPAIR_INSTRUCTION`:
append a `HumanMessage` with `GUIDANCE_MARKER` directly to `llm_messages` and
`continue` the existing while-loop inside `_call_llm`. This keeps repair within
a single `_call_llm` invocation.

Do **not** use `submit_guidance` / `_pending_guidance` for repair prompts. That
mechanism drains at the **next** `_call_llm` entry, which would require a full
graph round-trip (call_llm → router → execute_tools → call_llm) for each repair
attempt. The in-loop approach avoids that overhead and keeps the repair state
local to the current `_call_llm` call.

### State Storage

Missing-turn count and the pending provisional candidate are stored as local
variables inside `_call_llm`, **not** in `AgentState` or graph instance
attributes.

This works because:

- The missing-turn counter resets when a regular tool call routes to
  `execute_tools`. After tool execution, the graph re-enters `_call_llm` via
  the router, starting a fresh `_call_llm` invocation with count = 0. A regular
  tool call means the agent resumed material work, so resetting the count is
  correct behavior.
- The pending provisional candidate only needs to survive within a single
  `_call_llm` invocation (across in-loop repair retries). If the model calls a
  regular tool instead of `turn()`, the provisional text is discarded — the
  next `_call_llm` will produce a new response.

No new `AgentState` fields are needed for the barrier mode. The existing
`convergence_forced` field remains unchanged.

### Interaction with Convergence Mechanism

The convergence system (`convergence_forced` + `generate_fallback_summary`)
fires when step limits are reached. The turn-control barrier operates
independently and at a different layer:

- Convergence is a **step-budget** guard: it fires when `step_count` approaches
  `max_steps`, injecting a forced summary prompt.
- Turn-control is a **completion-protocol** guard: it fires when the model
  emits text without a tool call or `turn()`.

Priority: turn-control repair runs first (inside `_call_llm`). If the model
still hasn't committed after the third-miss fallback, `_call_llm` returns the
committed candidate as a tool-free `AIMessage`. The router sends it to
`_finalize`, where convergence may still append a fallback summary if
`convergence_forced` is set and the committed text is too short (< 20 chars).

In practice, if convergence forced a summary and the model emits that summary
as plain text without calling `turn()`, the turn-control repair will prompt the
model to call `turn()`. If the model still doesn't comply after two prompts,
the third-miss fallback commits the summary text. This is the correct outcome
— the convergence summary is a valid terminal response.

### `turn` Tool Definition Injection Point

Inject the `turn` tool definition in `_call_llm` after the
`tool_defs = self.tools.tools_for_llm()` call, before the
`filter_unavailable_lsp_tools` / `strip_gemini_unsupported_schema_keys` pipeline:

```python
tool_defs = self.tools.tools_for_llm()
if self._turn_control_enabled():
    tool_defs = [*tool_defs, TURN_TOOL_DEFINITION]
tool_defs = filter_unavailable_lsp_tools(tool_defs, ...)
tool_defs = strip_gemini_unsupported_schema_keys(tool_defs, ...)
```

This ensures subagents never see `turn` — `subagent.py` has its own separate
`tools_for_llm()` call (`subagent.py:95`) that does not go through this injection
point. No filtering is needed on the subagent path.

### Model Call Budget per `_call_llm` Invocation

The maximum model calls within a single `_call_llm` invocation in barrier mode:

| Path                          | Calls |
|-------------------------------|-------|
| Initial response              | 1     |
| Malformed tool call repair    | ≤ 2   |
| Missing-turn prompt 1         | +1    |
| Missing-turn prompt 2         | +1    |
| Invalid/mixed turn repair     | +1    |
| Context overflow compaction   | +1    |
| **Worst case total**          | **≤ 7** |

This is bounded and acceptable. The existing malformed-tool-call path already
allows up to 2 extra calls. The turn-control repair adds at most 3 more.

### Streaming UI Behavior for Provisional Text

When the model streams provisional text (no `turn()` call):

1. The text is rendered live via `StreamingRenderer` as normal.
2. The text remains visible in the UI.
3. If the model then calls a regular tool (resuming work), the provisional text
   stays visible but is **not** persisted as a terminal `AIMessage`. The tool
   executes normally, and the next `_call_llm` produces a new response.
4. If the model calls `turn()`, the existing streamed text is committed as the
   terminal `AIMessage`. No re-stream or duplication occurs.
5. If the third-miss fallback triggers, the latest streamed text is committed
   without a `turn()` call.

The UI does not need to distinguish provisional from committed text at the
streaming layer. The distinction is only in message persistence: provisional
text that gets superseded is simply not added to `state["messages"]` as a
terminal `AIMessage`.

### Normalization of Terminal `AIMessage`

When `turn()` is valid, `_call_llm` constructs the terminal `AIMessage` from
the pending provisional candidate:

```python
terminal_msg = AIMessage(
    content=pending_provisional.content,
    additional_kwargs={
        k: v for k, v in pending_provisional.additional_kwargs.items()
        if k != "tool_calls"
    },
)
```

This preserves provider metadata (usage, response metadata) while stripping
the `turn` tool call. The router then sees a tool-free `AIMessage` and routes
to `finalize → END` as before.

## Resolved Decisions

1. **Barrier mode default**: `barrier` mode is the default for OpenAI and
   Anthropic protocols. Models without reliable function/tool calling remain
   on `off`. No explicit opt-in config flag is required for the initial rollout;
   telemetry counters will track protocol failure rates to inform future
   adjustments.

2. **Repair prompt wording**: the prompt text specified in the Repair Behavior
   section is a starting point. Final wording will be iterated during
   implementation with prompt testing against real model behavior.

3. **Telemetry storage**: turn-control counters go to a dedicated metrics sink,
   separate from `_usage_stats`. This keeps turn-control protocol health metrics
   independent from token/cost accounting.

