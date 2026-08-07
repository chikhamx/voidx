# Agent Spawn and Control Results

Date: 2026-08-06

> **Status: Approved design; awaiting implementation**

## Goal

Make `agent` spawn results and `agent_control` operations concise, consistent, and predictable:

- identify a spawned child with the same stable display name used by later control results;
- expose the spawned `run_id` exactly once in LLM-visible output;
- accept one or multiple run IDs for `wait` and `cancel`;
- replace unbounded waiting with three finite tiers: 64, 128, and 256 seconds;
- keep visible output compact while preserving diagnostics in metadata;
- emit `next_step_hint` only when the caller needs actionable guidance.

## Current State

The implementation is centered in:

- `src/voidx/agent/adapters/tools/subagent_control.py` — input schema, wait/cancel execution, output, metadata, and hints;
- `src/voidx/agent/gateway/gateway.py` — single-run `wait` and `cancel` primitives;
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
- successful `agent` spawn output uses the internal agent name rather than the stable child display name;
- a spawned `run_id` is repeated in visible output, metadata, and `next_step_hint`.

## Non-goals

This change does not:

- add status-only polling or run discovery;
- change gateway routing or authorization rules;
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
- keep `run_id` and the full run record in metadata for programmatic consumers;
- do not repeat `run_id` in `next_step_hint`;
- do not add explanatory prose such as `Child agent ... spawned with ...`.

Result fields:

```text
title: Athena: <goal>
summary: Athena spawned
display: ""
metadata: unchanged existing spawn metadata
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

Single-run and batch calls use one internal execution path. Batch operations start concurrently with `asyncio.gather`; the wall-clock wait is bounded by the selected tier rather than multiplied by the number of runs.

The tool composes the existing gateway `wait` and `cancel` methods. No new gateway API is required.

Each item is isolated:

- one invalid, inaccessible, or unknown run does not prevent other items from completing;
- results are rendered in normalized input order, regardless of completion order;
- cancellation is also concurrent;
- a batch may therefore contain completed, running, failed, cancelled, and control-error items.

### Error classification

A child run with status `failed` is a child execution failure, not a gateway control failure. An exception raised while calling the gateway is a control error for that item.

For batch metadata:

- set `error=true` only when no item could be controlled;
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

Preserve the existing successful single-run fields:

```text
run
status
wait_outcome
terminal
result_quality
finish_reason
```

A single control error preserves:

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
            "terminal": True | False,
            "result_quality": "...",
            "finish_reason": "...",
        },
        {
            "run_id": "...",
            "status": "error",
            "error": True,
            "reason": "gateway_error",
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
- unknown, inaccessible, or unauthorized run:
  ```text
  Verify the run IDs and parent-child control relationship before retrying.
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

- Keep gateway route validation unchanged.
- Do not add `AgentGateway.wait_many` or `cancel_many`; concurrency belongs in the adapter for this scope.
- Do not serialize batch waits or cancellations.
- Do not let one per-item exception cancel sibling operations.
- Do not retain aliases for `brief` or `until_complete` in the public schema.
- Keep output rendering deterministic and in input order.
- Keep metadata structured; consumers must not need to parse visible output.
- Preserve the public `AgentInput` schema.
- Use the same stable display-name helper for spawn and control output.
- Expose a spawned run ID once in LLM-visible output and retain it in metadata.
- Preserve unrelated work in the currently dirty tree.

## Implementation Files

Modify:

- `src/voidx/agent/adapters/tools/subagent_control.py`
- `src/voidx/agent/adapters/tools/subagent.py`
- `src/voidx/presentation/output/tool_display.py`
- `src/tests/test_tooling/test_agent_control.py`
- `src/tests/test_tooling/test_interactive_tools.py`
- `src/tests/test_tooling/permission/test_permission_phase4.py`
- `src/tests/test_infrastructure/runtime/test_execute_tools_guard.py`
- `src/tests/test_presentation/output/test_tool_display.py`

Do not modify `src/voidx/agent/gateway/gateway.py` unless implementation evidence proves an adapter-only solution cannot satisfy the concurrency contract.

## TDD Plan

1. Add failing spawn-result tests for stable display names, a single LLM-visible `run_id`, preserved metadata, and exact success/error hints.
2. Implement the minimal `agent` spawn result changes without changing `AgentInput`.
3. Add failing schema and normalization tests for `str | list[str]`, the three tiers, empty lists, blank IDs, and stable deduplication.
4. Add failing behavior tests for concurrent batch wait and cancel, deterministic ordering, partial control errors, and top-level error classification.
5. Add failing output tests for completed, running, failed, cancelled, incomplete, and control-error items.
6. Add failing hint tests proving hints are absent on normal control outcomes and exact on timeout/error outcomes, including mixed-batch deduplication.
7. Implement the minimal control adapter changes to make those tests pass.
8. Add failing legacy-timeout mapping tests, then replace the old tier mapping.
9. Add failing batch display tests, then implement `<N> agents` rendering.
10. Update integrated tests and remove all repository references to the retired public values `brief` and `until_complete`.

Timeout tests must replace the timeout mapping or use a controlled fake gateway; they must not sleep for 64, 128, or 256 seconds.

## Verification

Focused verification:

```bash
./test.py --backend -- \
  src/tests/test_tooling/test_agent_control.py \
  src/tests/test_tooling/test_interactive_tools.py \
  src/tests/test_tooling/permission/test_permission_phase4.py \
  src/tests/test_infrastructure/runtime/test_execute_tools_guard.py \
  src/tests/test_presentation/output/test_tool_display.py \
  -v
```

Repository reference check:

```bash
rg -n 'brief|until_complete|Agent run status:|Do not call agent_control\(wait\) again' \
  src/voidx src/tests
```

Expected result: no live `agent_control` schema, call site, or output assertion retains the retired tiers or removed verbose output. Unrelated uses, if any, must be reviewed rather than changed mechanically.

## Acceptance Criteria

- Successful spawn output uses the stable child display name and exposes `run_id` exactly once to the LLM.
- Spawn metadata remains compatible, and spawn hints contain only actionable guidance.
- `run_id` accepts one non-empty string or a non-empty list of non-empty strings.
- Wait and cancel operate concurrently for multiple IDs and isolate per-item errors.
- Wait tiers are exactly `standard=64`, `extended=128`, and `maximum=256` seconds.
- No `agent_control` call can wait indefinitely.
- Visible output follows the compact per-item format and preserves input order.
- Existing successful single-run metadata remains compatible; batch metadata follows the `items` contract.
- `next_step_hint` is empty for normal outcomes and contains only actionable guidance for timeout or error/incomplete outcomes.
- Batch display does not expose raw run IDs.
- The focused backend verification command passes.
