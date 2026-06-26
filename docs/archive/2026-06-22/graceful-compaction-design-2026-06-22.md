# Graceful Context Compaction Design

Date: 2026-06-22

> **Status: Done — Phase 1 and Phase 2 implemented**

## Goal

Make context compaction a runtime-controlled preflight step that happens before
the next main LLM call. Compaction must reduce context pressure without changing
the semantic shape of the user's next request, leaking compaction artifacts into
the UI, or requiring the main model to successfully call a `compact` tool before
it can continue.

The desired user-visible behavior is simple: when the conversation is large,
voidx quietly summarizes older context, keeps recent tail messages, and then the
agent continues the current task normally.

## Problem

The current system has two compaction paths:

- dedicated compaction through `GraphCompactionCoordinator`;
- inline compaction through `VOIDX_COMPACTION_GUIDE`, where the main model is
  asked to call the hidden `compact` tool before continuing.

The inline path is fragile at exactly the moment compaction matters most. When
the context is near the limit, the main model can emit malformed or partial tool
call text, for example a legacy XML argument fragment ending in `</arg_value>`.
`stream_llm()` only parses complete DSML or legacy XML tool call blocks. If the
fragment does not become an `AIMessage.tool_calls` entry, `_router()` treats the
assistant message as a normal final answer and ends the turn.

This makes compaction visible and disruptive:

- partial tool-call text can be rendered in the terminal;
- no actual tool executes;
- the turn ends even though the user task is not complete;
- the next LLM call may replay the malformed fragment as assistant history.

## Design Summary

Compaction should be promoted to a deterministic preflight stage:

```text
turn start
  -> load persisted session messages
  -> append current user message
  -> estimate the next main-call context
  -> preflight compact if needed
  -> rebuild runtime context with the new summary
  -> call the main LLM with normal task messages only
```

The main model should not be asked to perform compaction as part of its normal
task loop. In the stable design, `VOIDX_COMPACTION_GUIDE` is disabled by
default. It can remain available later as an opt-in optimization, but correctness
must not depend on it.

This design is intentionally phased:

- **Phase 1**: preflight compaction, soft threshold, runtime-context rebuild,
  inline compaction disabled by default.
- **Phase 2**: malformed tool-call recovery for all tools.

## Trigger Timing

Use thresholds that run before the provider rejects the request:

| Trigger | Action |
|---------|--------|
| `soft_threshold` reached | Compact before the next main LLM call. |
| `hard_threshold` reached | Force compaction before any main LLM call. |
| Provider context overflow error | Compact, rebuild messages, and retry the same main LLM call. |
| Resume long session with no summary | Compact after loading history and appending the current user message, before graph invocation. |

Default threshold policy:

- `soft_threshold`: `min(0.75 * context_limit, usable_window())`.
- `hard_threshold`: existing overflow behavior, currently 90% of
  `context_limit`.
- `compaction_soft_ratio`: configurable, default `0.75`.
- `post_compaction_target_ratio`: configurable, default `0.10`.
- `resume_force_compact_message_count`: keep existing message-count guard, but
  route it through the same preflight path.

The soft threshold is based on `context_limit`, not a percentage of
`usable_window()`. With the default 128K context, 20K compaction buffer, and 8K
output reserve:

| Metric | Value |
|--------|-------|
| `context_limit` | 128,000 |
| `usable_window()` | 100,000 |
| `soft_threshold` | 96,000 |
| `hard_threshold` | 115,200 |
| `post_compaction_target` | 12,800 |

This avoids the overly aggressive 80-85K trigger while still leaving enough
headroom to compact before provider rejection. The cap at `usable_window()` keeps
small or high-output configurations from exceeding the budget reserved for model
output and compaction safety.

Compaction should be aggressive after it fires: the target is to shrink the
next main-call context to about 10% of `context_limit` while keeping the most
recent conversation intact. That means compaction should prefer deep pruning of
older turns and only retain as much older headroom as fits beneath the post-
compaction target.

## Message Flow

### Before Compaction

```text
System runtime context
older turns
recent tail turns
current user message
```

### Compaction Request

The dedicated compaction agent receives:

```text
selected older head messages
previous long summary, if any
structured compaction request
```

This request is never appended to the main task messages and is never persisted
as assistant/tool history.

### After Compaction

```text
System runtime context, rebuilt with Long Summary
recent tail turns
current user message
```

The runtime must rebuild the context after compaction. It should not simply
insert a temporary `SystemMessage("## Long Summary...")` and immediately call the
main model. Rebuilding through `RuntimeContextBuilder` keeps the next main LLM
call clean and makes the summary part of the normal stable context contract.

The current graph already has one pre-`graph.ainvoke()` compaction point in
`GraphTurnRunner.run_once()`. Phase 1 should preserve that placement and extend
it with the soft threshold. The larger behavior change is inside `_call_llm()`:
overflow recovery should no longer call `_in_turn_compact()` with
`include_summary_message=True` and then immediately continue with a temporary
summary message.

## Preflight Compaction Contract

Add a single preflight operation that can be called from turn start and from
main LLM overflow recovery:

```python
async def preflight_compact_if_needed(
    messages: list[BaseMessage],
    *,
    force: bool = False,
    reason: str = "threshold",
) -> PreflightCompactionResult:
    ...
```

The operation should:

1. Estimate the next main-call context using the same message shape that would
   normally be sent.
2. Decide whether compaction is needed using soft/hard/force rules.
3. Select head and tail with the existing `CompactionService.select_details()`.
4. Run the dedicated compaction agent, or use fallback summary if it fails.
5. Persist deleted head messages and runtime state.
6. Return the semantic tail and summary.
7. Require the caller to rebuild runtime context before the main LLM call.
8. Preserve the current user message in the returned tail.

### Tail Retention Rule

The returned tail must satisfy both of these constraints:

1. Keep at least the current interrupted turn and the immediately previous turn.
2. Keep as much additional recent conversation as possible, provided the
   rebuilt main-call context stays at or below `post_compaction_target_ratio`.

If the minimum two-turn tail already exceeds the post-compaction target, keep
those two turns anyway and rely on aggressive pruning of earlier tool outputs or
fallback summary shortening to recover budget.

This means the tail selection algorithm should work in this order:

1. Lock the current user turn and the previous turn into the tail.
2. Expand further backward only while the rebuilt context remains near the
   10% target.
3. Never remove the current user message from the tail.

The result should include structured metadata. Phase 1 can attach this to the
live compaction result before introducing any new persisted result-frame schema:

```python
class PreflightCompactionResult(BaseModel):
    compacted: bool
    summary: str = ""
    removed_message_count: int = 0
    retained_turn_count: int = 0
    pre_tokens: int = 0
    post_tokens: int = 0
    post_target_tokens: int = 0
    tail_anchor_id: str = ""
    fallback: bool = False
    reason: str = ""
```

### Relationship to Existing Methods

`_maybe_compact()` is already the closest existing API for pre-graph
compaction. Phase 1 should either evolve it into this preflight contract or wrap
it with a clearer `preflight_compact_if_needed()` method. The important behavior
change is not the placement in `turn_runner.py`; it is:

- add soft-threshold triggering before `graph.ainvoke()`;
- make overflow recovery in `_call_llm()` use the same preflight-style result;
- remove `_in_turn_compact()` as the normal overflow recovery path;
- stop using `include_summary_message=True` for main-call recovery.

Manual `/compact` can continue to use session-history compaction.

### Extreme Compression Target

Phase 1 should not stop at "just enough to avoid overflow." The default target
is deliberately aggressive:

- shrink the next main-call context to roughly 10% of `context_limit`;
- keep the most recent dialogue possible inside that budget;
- preserve at minimum the current turn and the previous turn;
- if the two-turn minimum exceeds the target, honor the two-turn minimum and
  compress earlier context harder.

The implementation should expose this as configuration rather than a hard-coded
constant so the default can be tuned later without changing the control flow.

## Main LLM Call Contract

The main LLM call must only receive task-relevant messages:

- allowed: runtime system prompt, long summary, workflow context, recent tail,
  current user request, normal guidance;
- disallowed: compaction request prompt, `VOIDX_COMPACTION_GUIDE`, `compact`
  tool result, hidden compaction control messages.

After preflight compaction, `_call_llm()` should rebuild `llm_messages` from the
new semantic tail rather than reuse the pre-compaction list.

### Rebuild Mechanics

There are two rebuild paths:

1. **Turn-start preflight**: compact before `graph.ainvoke()`. The compacted
   message list is then passed into the normal graph. `_prepare_with_stream()`
   consumes `_pending_summary` or `_compaction_summary` and rebuilds the runtime
   context through `RuntimeContextBuilder`.
2. **Main-call overflow recovery**: `_call_llm()` detects that the prepared
   `llm_messages` are too large or catches a provider overflow. It should run
   preflight compaction, update `_compaction_summary`, rebuild the runtime
   context for the current state, and only then retry the main LLM call.

The second path should prefer a helper that rebuilds the prepared state through
the same `RuntimeContextBuilder` logic used by `_prepare_with_stream()`. If that
is too invasive for the first patch, an intermediate helper may update the
current `SystemMessage` with the new summary, but it must be explicitly treated
as a temporary migration step and covered by tests. The target behavior is one
runtime-context rebuild path, not ad hoc summary insertion.

`rebuild_llm_messages()` should accept an `allow_inline_compaction` flag:

```python
def rebuild_llm_messages(
    messages: list[BaseMessage],
    *,
    allow_inline_compaction: bool,
) -> tuple[list[BaseMessage], list[HumanMessage], bool]:
    ...
```

`allow_inline_compaction` must be false when:

- inline compaction is disabled by config;
- `compaction_happened` is true;
- the call is retrying after a provider overflow;
- the call is recovering from preflight compaction.

## Inline Compaction Policy

Default:

```text
enable_inline_compaction = false
```

Inline compaction can be reintroduced later under a feature flag. If enabled,
it must obey these rules:

- only run below the soft threshold, where the model still has comfortable
  context headroom;
- never run after a provider overflow error;
- never be required for correctness;
- if the model emits malformed compaction/tool-call text, fall back to
  dedicated preflight compaction instead of ending the turn.

## Phase 2: Malformed Tool-Call Recovery

Malformed tool-call recovery is useful, but it is orthogonal to graceful
compaction and affects all tool calls. It should be implemented after Phase 1,
or in a separate design if the changes grow beyond a small guard.

The streaming layer should eventually recognize assistant output that looks like
a partial tool call but did not parse into `tool_calls`.

Examples:

- contains `<tool_call`, `</tool_call>`, `<arg_key>`, `<arg_value>`, or
  `</arg_value>`;
- contains DSML tool-call markers but does not parse into a complete call;
- contains provider-specific tool-call JSON fragments with no parsed tool call.

When this happens:

1. Do not render the fragment as user-visible assistant text.
2. Mark the response as `malformed_tool_call`.
3. In `_call_llm()`, retry once with a short repair instruction:
   "Your previous response looked like an incomplete tool call. Re-emit a valid
   tool call using the bound tool schema, or answer normally without tool-call
   markup."
4. If retry fails and compaction was pending or recently happened, run
   dedicated preflight compaction and retry once more.
5. If recovery still fails, end with an explicit assistant error explaining
   that the model returned an invalid tool call, rather than silently ending the
   turn with markup fragments.

This recovery path protects all tool calls, not only compaction.

## UI Behavior

Compaction status should be concise:

- start: `Compacting context`;
- finish: `Compacted N old messages`;
- fallback: `Compaction fallback used`;
- failure: explicit warning only if fallback cannot produce a usable summary.

Hidden compaction artifacts must not appear in the transcript:

- no `VOIDX_COMPACTION_GUIDE`;
- no partial XML/DSML fragments from compaction-related output;
- no `compact` tool result unless debug logging explicitly asks for it.

Debug logs and context frames may still record compaction input metadata.
General suppression of malformed tool-call fragments belongs to Phase 2.

## Persistence

When compaction succeeds or fallback summary is used:

- save `_compaction_summary` in runtime state;
- delete persisted messages through the selected head boundary;
- update `_session_msg_cache` and `_context_cache`;
- save a compaction context frame with `frame_kind="compaction"`;
- save the next main context frame after rebuilding runtime context.

The persisted transcript should represent the user's conversation, not the
internal compaction request.

Compaction result metadata should include the budget decision so future
debugging can distinguish normal aggressive compaction from accidental context
loss. Persisting the completed result metadata into a dedicated context frame can
be added later without changing the Phase 1 control flow:

```json
{
  "compaction_reason": "soft_threshold|hard_threshold|provider_overflow|resume",
  "pre_tokens": 104000,
  "post_tokens": 12600,
  "soft_threshold": 96000,
  "hard_threshold": 115200,
  "post_compaction_target": 12800,
  "removed_message_count": 38,
  "retained_turn_count": 2,
  "current_user_preserved": true,
  "inline_compaction_enabled": false
}
```

## Error Handling

| Failure | Handling |
|---------|----------|
| Compaction agent returns no summary | Use deterministic fallback summary and continue. |
| Compaction agent raises provider error | Retry according to existing compaction retry policy, then fallback. |
| Fallback summary cannot be built | Emit explicit warning and continue with pruned tail only if token budget allows. |
| Main LLM still overflows after compaction | Increase tail trimming, rebuild, and retry once. |
| Main LLM returns malformed tool-call text | Phase 2: suppress fragment, retry repair, then return explicit failure if needed. |

The user turn should not end silently because compaction failed.

## Testing

Focused tests should cover:

| Test | Purpose |
|------|---------|
| `test_preflight_compaction_runs_before_main_llm_call` | Main model receives rebuilt messages with summary and no compaction guide. |
| `test_preflight_compaction_rebuilds_runtime_context` | Summary appears in runtime context, not as a stray temporary message. |
| `test_preflight_compaction_preserves_current_user_message` | Current user request remains in the tail and reaches the main LLM unchanged. |
| `test_preflight_compaction_targets_ten_percent_context` | Compaction keeps post tokens near 10% when older turns can be summarized. |
| `test_preflight_compaction_keeps_minimum_two_turn_tail` | Current interrupted turn and previous turn are preserved even when target pressure is high. |
| `test_preflight_compaction_expands_tail_until_target` | Additional recent turns are retained only while the rebuilt context stays within target. |
| `test_inline_compaction_disabled_by_default` | `_inline_compaction_guide_for()` returns `None` unless feature flag is enabled. |
| `test_context_overflow_compacts_and_retries_same_call` | Overflow error triggers compaction, rebuild, retry. |
| `test_resume_long_session_uses_preflight_path` | Long-session resume uses the same preflight compaction contract. |
| `test_malformed_tool_call_does_not_end_turn` | Phase 2: partial XML/DSML response is retried or converted to explicit failure, not treated as final answer. |
| `test_malformed_tool_call_not_rendered` | Phase 2: UI renderer suppresses partial tool-call fragments. |

## Non-goals

- Do not redesign the summary format.
- Do not change how head/tail selection works unless tests reveal a specific
  bug.
- Do not remove the `compact` tool; keep it available for future opt-in inline
  optimization.
- Do not persist compaction prompts as normal conversation messages.
- Do not make compaction ask the user for confirmation by default.
- Do not change subagent context management in Phase 1. Subagents keep their
  existing independent context behavior; any subagent-specific compaction policy
  should be designed separately.
- Do not implement full malformed tool-call recovery in Phase 1.

## Implementation Notes

Likely touch points:

- `src/voidx/agent/graph/turn_runner.py` - run preflight compaction before
  `graph.ainvoke()` using the existing placement, with soft-threshold support.
- `src/voidx/agent/graph/core/llm.py` - remove default inline guide injection,
  skip inline guide when compaction happened or is disabled, and rebuild runtime
  context after overflow compaction.
- `src/voidx/agent/graph/compaction_coordinator.py` - expose a preflight-shaped
  API around existing compaction behavior, including the post-compaction target
  and two-turn minimum tail rule.
- `src/voidx/agent/graph/compaction.py` - replace `_in_turn_compact()` usage in
  main-call overflow recovery; avoid `include_summary_message=True` on the
  target path.
- `src/voidx/llm/compaction.py` - update tail selection or add a preflight
  selection helper that targets 10% context while preserving the two-turn
  minimum.
- `src/voidx/agent/graph/streaming.py` - Phase 2: detect and suppress malformed
  tool-call fragments.
- `tests/test_agent/test_call_llm_compaction.py` and
  `tests/test_agent/test_stream_llm_sanitization.py` - add regression coverage.
