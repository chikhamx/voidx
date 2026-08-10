# Agent Spawn and Control Results

> **Status: Done** — Archived on 2026-08-09.

Date: 2026-08-06
Revised: 2026-08-09

> **Status: Approved design; revised after technical review; awaiting implementation**

## Goal

Make `agent` spawn results and `agent_control` operations concise, consistent, and predictable:

- identify a spawned child with the same stable display name used by later control results;
- expose the spawned `run_id` exactly once in LLM-visible output;
- accept one or multiple run IDs for `wait` and `cancel`;
- replace unbounded waiting with three finite tiers: 64, 128, and 256 seconds;
- bound cancellation acknowledgement so a non-cooperative child cannot block control forever;
- keep visible output compact while preserving diagnostics in metadata;
- emit `next_step_hint` only when the caller needs actionable guidance.

## Current State

The implementation is centered in:

- `src/voidx/agent/adapters/tools/subagent_control.py` — input schema, wait/cancel execution, output, metadata, and hints;
- `src/voidx/agent/adapters/subagent/inprocess_gateway.py` — single-run `wait` and `cancel` primitives;
- `src/voidx/agent/domain/subagent.py` — run states, route policy, and gateway error contract;
- `src/voidx/agent/ports/subagent.py` — subagent transport protocol;
- `src/voidx/agent/adapters/tools/subagent.py` — legacy control-argument normalization;
- `src/voidx/presentation/output/tool_display.py` — tool header display values;
- `src/tests/test_tooling/test_agent_control.py` — focused control-tool behavior;
- `src/tests/test_tooling/test_interactive_tools.py` — integrated spawn/control behavior.

Current limitations:

- `run_id` accepts only one string;
- `wait` uses `brief=5s`, `extended=30s`, and unbounded `until_complete`;
- visible wait output repeats status and diagnostic fields already available in metadata;
- terminal success still emits a hint telling the model not to wait again;
- one gateway exception terminates the whole tool call;
- `cancel` awaits the child task without a deadline and can block forever if the child suppresses cancellation;
- gateway control errors expose only message text, so the adapter cannot classify recovery guidance reliably;
- successful `agent` spawn output uses the internal agent name rather than the stable child display name;
- a spawned `run_id` is repeated in visible output, metadata, and `next_step_hint`.

## Non-goals

This change does not:

- add status-only polling or run discovery;
- change gateway routing or authorization decisions;
- change child-agent lifecycle statuses;
- add an unbounded wait mode;
- retry failed control operations automatically;
- include run status, elapsed time, IDs, or counts in `next_step_hint`.

## Agent Spawn Result Contract

The public `AgentInput` schema remains unchanged. This section changes only the result returned after attempting to spawn a child.

### Successful spawn

Derive the stable child display name from the new run ID using the same `subagent_display_name` helper used by `agent_control`.

Visible output:

```text
Athena [running]
run_id: run_xxx
```

Rules:

- show the stable child display name rather than the internal agent definition name;
- expose `run_id` once in LLM-visible content so a later control call can reference it;
- preserve the existing spawn metadata fields exactly: `agent`, `run_id`, and `status`;
- do not repeat `run_id` in `next_step_hint`;
- do not add explanatory prose such as `Child agent ... spawned with ...`.

Result fields:

```text
title: Athena: <goal>
summary: Athena spawned
display: ""
metadata: {"agent": "<internal definition name>", "run_id": "run_xxx", "status": "running"}
```

A successful asynchronous spawn is the one normal outcome that retains a hint because follow-up control is still available:

```text
Use agent_control(action='wait', wait='standard') when the result is needed, or continue with other independent work.
```

The hint is action-only: it does not repeat the display name, status, run ID, or timeout duration.

### Spawn errors

Keep the visible error concise and retain `metadata.error=true`. Add only the applicable recovery guidance:

- invalid arguments or delegation rejection:
  ```text
  Correct the arguments before retrying.
  ```
- gateway unavailable:
  ```text
  Restore agent gateway availability before retrying.
  ```
- resolver or runner unavailable:
  ```text
  Restore child-agent execution availability before retrying.
  ```
- spawn timeout or unexpected exception:
  ```text
  Inspect the error before starting a replacement run.
  ```

Do not emit a hint when it would only restate the error without identifying a concrete recovery action.

## Input Contract

```python
class AgentControlInput(BaseModel):
    action: Literal["wait", "cancel"]
    run_id: str | list[str]
    wait: Literal["standard", "extended", "maximum"] = "standard"
```

### Wait tiers

| Value | Timeout | Meaning |
|---|---:|---|
| `standard` | 64 seconds | Default wait for ordinary child-agent work. |
| `extended` | 128 seconds | Additional wait when the result is still needed. |
| `maximum` | 256 seconds | Final permitted wait; no further waiting should be recommended. |

Rules:

- `cancel` ignores `wait`.
- A string is normalized to a one-item list internally.
- A list must not be empty.
- Every run ID must be a non-empty string after trimming.
- Duplicate IDs are removed while preserving first-occurrence order.
- The public schema exposes no infinite-wait option.

## Execution Semantics

### Single and batch behavior

Single-run and batch calls use one internal execution path. Each per-item coroutine converts its own exception into an item result before `asyncio.gather` returns the ordered list. Batch operations start concurrently; the wall-clock wait is bounded by the selected tier rather than multiplied by the number of runs.

The adapter composes the transport's existing single-run `wait` and `cancel` methods. No `wait_many` or `cancel_many` API is added.

Each item is isolated:

- one invalid, inaccessible, or unknown run does not prevent other items from completing;
- results are rendered in normalized input order, regardless of completion order;
- cancellation requests also start concurrently;
- a batch may therefore contain pending, running, completed, failed, cancelled, and control-error items;
- `pending` follows the same nonterminal rendering and hint rules as `running`.

### Bounded cancellation

A cancel call must not wait indefinitely for the child task to acknowledge cancellation. Define `_CANCEL_ACK_TIMEOUT = 5.0` in `inprocess_gateway.py` and apply it after requesting task cancellation. The implementation must use a bounded primitive such as `asyncio.wait`; it must not use bare `await target.task` or `asyncio.wait_for(target.task, ...)`, because a task that suppresses cancellation can keep `wait_for` pending past its nominal timeout:

- if the child reaches a terminal state before the deadline, return that terminal run;
- if the child is still non-terminal when the deadline expires, raise `AgentGatewayError("Child cancellation was not acknowledged", reason="cancel_timeout")` for that item;
- do not force the run to `cancelled` unless task termination is observed;
- the adapter isolates this item error so sibling cancellation requests can still complete;
- the fixed cancellation deadline is internal and is not selected through the public `wait` field.

This changes cancellation completion mechanics only; it does not add a lifecycle status or weaken route checks.

### Structured gateway control errors

Extend `AgentGatewayError` with a stable `reason` attribute while preserving its human-readable message. Gateway control paths use these reasons:

| Reason | Meaning |
|---|---|
| `unknown_run` | A referenced requester or target run does not exist. |
| `route_not_allowed` | The requester is not allowed to control the target. |
| `cross_session` | Requester and target belong to different sessions. |
| `cancel_timeout` | The child did not acknowledge cancellation before the internal deadline. |
| `gateway_error` | Unexpected transport failure without a more specific reason. |

`AgentGatewayError(message, *, reason="gateway_error")` keeps existing one-argument call sites compatible. For `AgentGatewayError`, the adapter uses `exc.reason`; any non-`AgentGatewayError` exception is classified as `gateway_error`. It never derives a category by parsing exception text. Existing exception messages remain readable and route validation decisions remain unchanged.

### Error classification

A child run with status `failed` is a child execution failure, not a gateway control failure. An exception raised while calling the gateway is a control error for that item.

For batch metadata:

- an item is “controlled” only when its transport call returns an `AgentRun` without exception;
- set `error=true` only when no item was controlled;
- set `partial_error=true` when at least one item has a control error and at least one item was controlled;
- child status `failed` remains represented by its item status and does not by itself set top-level `error=true`.

Global failures such as invalid input or an unavailable gateway remain ordinary tool errors with `error=true`.

## Visible Output

Use the stable child display name derived from each run ID. Do not expose internal wait diagnostics in the visible text.

Each item uses this shape:

```text
Athena [completed]
Result:
<result>

Orion [running]

Nova [failed]
Error: <child error>

Lyra [cancelled]

Vega [error]
Error: <control error>
```

If a completed result has a non-normal `finish_reason`, append it to the heading:

```text
Cipher [completed; finish_reason=contract_unsatisfied]
Result:
<partial result>
```

Rendering rules:

- separate batch items with one blank line;
- omit `Result:` when no result text exists;
- omit placeholder text such as `No final result is available yet.`;
- show `Error:` for child failures and control errors;
- do not render `terminal`, `wait_outcome`, or `result_quality` in visible output;
- do not render explanatory prose about cached terminal results or repeat polling.

`display` and `summary` remain concise:

- single item: stable display name plus status;
- batch: item count in `display`, and status counts in `summary`.

## Metadata Contract

### Single item

A successful single-item `wait` preserves the existing fields:

```text
run
status
wait_outcome
terminal
result_quality
finish_reason
```

A successful single-item `cancel` preserves its existing fields:

```text
run
status
```

A single per-item control error returns:

```text
error=true
reason
detail
run_id
```

### Batch

Return:

```python
{
    "action": "wait" | "cancel",
    "items": [
        {
            "run_id": "...",
            "run": {...},
            "status": "...",
            "wait_outcome": "...",       # wait only
            "terminal": True | False,      # wait only
            "result_quality": "...",      # wait only
            "finish_reason": "...",       # wait only
        },
        {
            "run_id": "...",
            "status": "error",
            "error": True,
            "reason": "unknown_run" | "route_not_allowed" | "cross_session" | "cancel_timeout" | "gateway_error",
            "detail": "...",
        },
    ],
    "counts": {"completed": 1, "running": 1, "error": 1},
    "partial_error": True,               # only when applicable
    "error": True,                       # only when every item is a control error
}
```

Metadata is the source of truth for programmatic consumers; visible output must not duplicate diagnostics solely for machine parsing.

## `next_step_hint` Contract

For `agent_control`, a hint contains only actionable guidance. It must not repeat run status, duration, tier name as narrative, run IDs, or item counts. The successful `agent` spawn hint is the explicit normal-outcome exception defined above.

No hint is emitted for:

- normal completion;
- normal cancellation;
- an already-terminal cached result;
- a still-running result that did not time out;
- any outcome that requires no recovery action.

### Timeout guidance

When one or more runs time out:

- `standard`:
  ```text
  Use wait='extended' if the result is still needed; otherwise continue with other work.
  ```
- `extended`:
  ```text
  Use wait='maximum' only if the result is still needed; otherwise cancel the unfinished work or continue without it.
  ```
- `maximum`:
  ```text
  Do not wait again; cancel the unfinished work or continue without it.
  ```

### Error guidance

- invalid arguments:
  ```text
  Correct the arguments before retrying.
  ```
- gateway unavailable:
  ```text
  Restore agent gateway availability before retrying.
  ```
- `unknown_run`, `route_not_allowed`, or `cross_session`:
  ```text
  Verify the run IDs and parent-child control relationship before retrying.
  ```
- `cancel_timeout`:
  ```text
  Cancellation was not acknowledged; do not retry automatically, and report that the run may still be active.
  ```
- child execution failure:
  ```text
  Inspect the error and start a replacement run if the task is still needed.
  ```
- incomplete execution such as `contract_unsatisfied`:
  ```text
  Use the partial result if sufficient; otherwise start a narrower replacement task.
  ```

For mixed batch outcomes, collect all applicable guidance in the order below and remove duplicates:

1. timeout guidance;
2. control-error guidance;
3. child-failure guidance;
4. incomplete-execution guidance.

## Legacy Control Compatibility

`src/voidx/agent/adapters/tools/subagent.py` currently maps legacy raw `action`, `target_run_id`, and `timeout` arguments into `agent_control`. These fields are an internal compatibility path and must not be added back to the public `AgentInput` schema.

Update that mapping as follows:

| Legacy timeout | New tier |
|---|---|
| omitted or `0` | `maximum` |
| `0 < timeout <= 64` | `standard` |
| `64 < timeout <= 128` | `extended` |
| `timeout > 128` | `maximum` |

Values above 128 seconds are capped by the 256-second `maximum` tier. Negative or non-numeric values return an invalid-argument error. This compatibility path must never produce an unbounded wait.

## Tool Display

Update `src/voidx/presentation/output/tool_display.py` so that:

- a single run ID displays its stable child display name as today;
- multiple run IDs display `<N> agents`;
- raw run IDs are not exposed in the tool header;
- wait and cancel headers remain distinguishable by action.

## Implementation Constraints

- Keep gateway route validation decisions unchanged.
- Add stable reasons to existing gateway errors; do not infer categories from message text.
- Do not add transport-level `wait_many` or `cancel_many`; concurrency belongs in the adapter for this scope.
- Do not serialize batch waits or cancellations.
- Bound both wait and cancel paths; one per-item exception or cancellation timeout must not block sibling operations.
- Do not retain aliases for `brief` or `until_complete` in the public schema.
- Keep output rendering deterministic and in input order.
- Keep metadata structured; consumers must not need to parse visible output.
- Preserve the public `AgentInput` schema.
- Preserve spawn metadata as exactly `agent`, `run_id`, and `status`.
- Use the same stable display-name helper for spawn and control output.
- Expose a spawned run ID once in LLM-visible output and retain it in metadata.
- Preserve unrelated work in the currently dirty tree.

## Implementation Files

Modify:

- `src/voidx/agent/domain/subagent.py`
- `src/voidx/agent/adapters/subagent/inprocess_gateway.py`
- `src/voidx/agent/adapters/tools/subagent_control.py`
- `src/voidx/agent/adapters/tools/subagent.py`
- `src/voidx/presentation/output/tool_display.py`
- `src/tests/test_agent/adapters/subagent/test_inprocess_gateway.py`
- `src/tests/test_agent/application/subagent/test_policy.py`
- `src/tests/test_agent/adapters/langgraph/runtime/test_execute_tools_guard.py`
- `src/tests/test_tooling/test_agent_control.py`
- `src/tests/test_tooling/test_interactive_tools.py`
- `src/tests/test_tooling/test_tool_schemas.py`
- `src/tests/test_tooling/permission/test_permission_phase4.py`
- `src/tests/test_presentation/output/test_tool_display.py`
- `scripts/repro_wait_blocking.py`
- `scripts/repro_message_receive_blocking.py`

Do not create `src/voidx/agent/gateway/`; architecture tests require the transport implementation to remain in the adapter package.

## TDD Plan

1. Add failing spawn-result tests for stable display names, a single LLM-visible `run_id`, the exact existing three-field metadata contract, and exact success/error hints.
2. Implement the minimal `agent` spawn result changes without changing `AgentInput` or adding spawn metadata fields.
3. Add failing `AgentGatewayError.reason` tests for unknown run, disallowed route, and cross-session control while preserving existing messages and decisions.
4. Implement structured gateway error reasons without changing route authorization policy.
5. Add a failing gateway test with a child that suppresses `CancelledError`; patch `_CANCEL_ACK_TIMEOUT` to a short value and prove `cancel` returns `cancel_timeout` within the deadline without reporting the run as cancelled.
6. Implement the minimal bounded-cancel gateway change, then test normal and already-terminal cancellation regressions.
7. Add failing schema and normalization tests for `str | list[str]`, the three wait tiers, empty lists, blank IDs, and stable deduplication.
8. Add failing behavior tests for concurrent batch wait and cancel, deterministic ordering, partial control errors, cancel timeouts, and top-level error classification.
9. Add failing output tests for completed, running, failed, cancelled, incomplete, and control-error items.
10. Add failing hint tests proving hints are absent on normal control outcomes and exact on wait timeout, cancel timeout, structured gateway error, and mixed-batch outcomes.
11. Implement the minimal control adapter changes to make those tests pass.
12. Add failing legacy-timeout mapping tests, then replace the old tier mapping.
13. Add failing batch display tests, then implement `<N> agents` rendering.
14. Update integrated tests and live scripts; remove all live repository references to retired public values and removed verbose output.

Timeout tests must patch timeout constants or use controlled fake transports; they must not sleep for 5, 64, 128, or 256 seconds.

## Verification

Focused verification:

```bash
./test.py --backend -- \
  src/tests/test_agent/adapters/subagent/test_inprocess_gateway.py \
  src/tests/test_agent/application/subagent/test_policy.py \
  src/tests/test_agent/adapters/langgraph/runtime/test_execute_tools_guard.py \
  src/tests/test_tooling/test_agent_control.py \
  src/tests/test_tooling/test_interactive_tools.py \
  src/tests/test_tooling/test_tool_schemas.py \
  src/tests/test_tooling/permission/test_permission_phase4.py \
  src/tests/test_presentation/output/test_tool_display.py \
  -v
```

Architecture verification:

```bash
./test.py --backend -- src/tests/test_architecture/test_p5_boundaries.py -v
```

Repository reference check:

```bash
grep -RInE 'brief|until_complete|Agent run status:|Do not call agent_control\(wait\) again' \
  src/voidx src/tests scripts
```

Expected result: no live `agent_control` schema, call site, output assertion, or diagnostic script retains the retired tiers or removed verbose output. Unrelated natural-language uses of the word `brief` must be reviewed rather than changed mechanically.

## Acceptance Criteria

- Successful spawn output uses the stable child display name and exposes `run_id` exactly once to the LLM.
- Spawn metadata remains exactly `agent`, `run_id`, and `status`, and spawn hints contain only actionable guidance.
- `run_id` accepts one non-empty string or a non-empty list of non-empty strings.
- Wait and cancel start concurrently for multiple IDs and isolate per-item errors.
- Wait tiers are exactly `standard=64`, `extended=128`, and `maximum=256` seconds.
- No `agent_control` wait can block indefinitely.
- Cancellation acknowledgement is bounded by 5 seconds; a non-acknowledging child yields `cancel_timeout` and is not falsely reported as cancelled.
- Gateway control failures carry stable reasons, and the adapter does not classify them by parsing messages.
- Visible output follows the compact per-item format and preserves input order.
- Existing successful single-run metadata remains compatible; batch metadata follows the `items` contract.
- `next_step_hint` is empty for normal outcomes and contains only actionable guidance for timeout or error/incomplete outcomes.
- Batch display does not expose raw run IDs.
- Focused and architecture verification commands pass.
