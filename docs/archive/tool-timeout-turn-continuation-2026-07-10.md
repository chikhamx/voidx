# Unified Tool Timeout Contract and Turn Continuation

> **Status: Done** — Archived on 2026-07-11.

**Date:** 2026-07-10  
**Status:** Revised / Awaiting approval — not implemented

## TL;DR

Ordinary tool execution timeouts become recoverable error results that return control to
the model. UI tool-start notification timeout remains terminal, but termination is
authorized only by private executor state—not tool-supplied metadata. The executor fails
fast by cancelling and awaiting in-flight siblings, skips later groups and barriers while
still completing every tool-call result, bypasses unbounded UI queue draining on the
trusted terminal path, and excludes infrastructure failures from runtime guards. All timeout-capable adapters adopt one diagnostic metadata contract.

## Document State and Review Gate

This document is a pre-implementation technical design. References to current behavior and
code paths are evidence for the problem and architectural boundaries; statements using
“must”, “will”, “add”, or “target” describe changes that do not exist yet.

Design approval does not require the proposed fields, helpers, files, or tests to already
be present. The design quality gate is:

- current behavior and referenced implementation boundaries are accurate;
- target behavior, ownership, ordering, cleanup, and failure semantics are unambiguous;
- every target change has a concrete source path and deterministic test coverage;
- the implementation order is dependency-correct; and
- the post-implementation verification commands are complete.

The focused and backend commands in **Verification** are acceptance commands to run after
implementation. During design review, planned new paths such as
`src/voidx/runtime/processes.py` and `src/tests/test_runtime/test_processes.py` are expected
to be absent.

## Context

Shell tools return a structured timeout failure when their subprocess exceeds the configured limit:

```python
ToolResult(
    output=...,
    metadata={
        "command": command,
        "exit_code": -1,
        "timeout": True,
        "error": True,
    },
)
```

The graph executor currently uses the same generic `metadata.timeout` flag to detect a
`UiEventBus.request` timeout while notifying the frontend that a tool has started. As a
result, any Bash or PowerShell command timeout is treated as an unresponsive frontend:

1. The tool returns a normal timeout error result.
2. `execute_tools` sees `metadata.timeout`.
3. It appends a misleading `UI event bus timed out` assistant message.
4. It sets `should_continue=False`.
5. `route_after_execute_tools` routes directly to `end`.

The model never receives the timeout result in a follow-up LLM call, so it cannot narrow
the command, increase the timeout, choose another verification method, or explain the
failure. The session remains usable, but the current turn ends immediately and appears
to have been interrupted.

The behavior was introduced by commit `5e9a71fe` as a turn-level escape hatch for a
stalled UI event bus. That protection remains necessary; the bug is that the
implementation does not distinguish infrastructure notification timeouts from ordinary
tool execution timeouts.

Other timeout-capable adapters currently avoid the turn-termination bug only because
they do not set `metadata.timeout`:

- MCP catches `McpTimeoutError` together with connection and protocol failures.
- Git emits `returncode=-1` and a timeout string.
- LSP converts request timeout into a generic `LspConnectionError`.
- Web tools retry network timeouts and then return a generic error.
- Agent delegates return all runner exceptions as generic errors.

These paths continue the turn today, but the model and runtime guards cannot reliably
distinguish a timeout from other failures. This specification therefore covers both the
turn-routing bug and a unified timeout result contract across tool adapters.

## Goal

Treat an ordinary tool execution timeout as a recoverable tool failure within the
current turn:

- Return the timeout as an error `ToolMessage`.
- Return consistent structured timeout metadata from every adapter that can identify a
  timeout.
- Route back to the LLM after tool execution.
- Allow the model to continue, retry with different parameters, use another tool, or
  report the failure.
- Continue terminating the turn when `UiEventBus.request` times out while notifying the
  frontend of a tool start.
- Cancel and await in-flight sibling tools without leaking child processes, local LSP or
  MCP stdio servers, background reader tasks, heartbeats, workspace locks, or per-file
  read/write locks.
- Preserve the originating adapter in metadata for diagnostics and runtime guards.

## Non-Goals

- Do not change the default Bash or PowerShell timeout of 120 seconds.
- Do not automatically retry timed-out tools.
- Do not change normal completion or deadline-timeout result semantics. Process-launch
  options and cleanup may change where required to guarantee full process-tree termination
  during executor fail-fast cancellation.
- Do not make `UiEventBus` self-healing after a stalled consumer.
- Do not migrate `selfupdate.py` to the shared process lifecycle helper; self-update is not
  executed as a sibling tool in the graph batch covered by this specification.
- Do not change provider-level LLM timeout and retry behavior.
- Do not change runtime guard thresholds for repeated failures or no-progress cycles.
- Do not modify the turn-control completion protocol.
- Do not infer timeouts by parsing arbitrary third-party tool output.
- Do not classify user cancellation in `clarify` or `checkpoint` as a tool execution
  timeout. Their interaction layer currently exposes cancellation but not a reliable
  timeout reason.

## Timeout Taxonomy

| `error_kind` | Meaning | Turn behavior |
|---|---|---|
| `tool_timeout` | A tool operation, subprocess, remote call, language-server request, network request, or child runner exceeded its execution deadline | Return error to LLM and continue |
| `ui_event_bus_timeout` | The orchestration layer could not notify the UI that a tool was starting | Terminate the current turn to avoid a hang |
| `interaction_timeout` | Reserved for a future user-interaction response that distinguishes timeout from explicit cancellation | Return interaction result to LLM; do not terminate |
| Provider/LLM timeout | Model request timeout handled by the LLM retry loop, not a tool result | Existing provider retry behavior |

All tool execution timeouts use the same `error_kind="tool_timeout"`. Adapter identity
is recorded separately as `timeout_source`.

## Adapter Coverage

| Adapter | Current timeout representation | Required representation |
|---|---|---|
| Bash / PowerShell | `timeout=True`, `error=True` | Add `error_kind="tool_timeout"`, `timeout_source="shell"` |
| MCP tool wrapper | `McpTimeoutError` folded into generic unavailable error | Catch separately and return unified timeout metadata with source `mcp` |
| MCP stdio lifecycle | Process creation/handshake cancellation is not transactionally cleaned up | Use the shared owned-process helper; cancel and await reader/stderr tasks and the full process tree before propagating cancellation |
| MCP-backed Web Search / Fetch | All manager exceptions folded into generic MCP web error | Catch MCP timeout separately and return source `mcp` |
| Git | `returncode=-1` and timeout text | Preserve a structured timeout flag from process runner through `GitTool`, source `git` |
| LSP | Request timeout converted to generic `LspConnectionError`; startup cancellation can bypass cleanup | Preserve `LspTimeoutError`; use transactional owned-process startup and clean initialization/background tasks before propagating cancellation |
| Direct Web Fetch / Search | `httpx.TimeoutException` retried, then folded into generic error | After retries/fallbacks are exhausted, return source `web` |
| Agent delegate | Runner exceptions folded into generic error | Catch explicit `TimeoutError` separately, source `agent` |
| Clarify / Checkpoint | Interaction timeout appears as cancelled response | Keep current cancellation behavior until the interaction API exposes a timeout reason |
| File, search, todo, workflow, document, compact, skills | No execution timeout boundary | No adapter change; global routing contract still applies to future timeout results |

## Behavioral Contract

| Scenario | Tool result | Trusted executor state | Expected graph behavior |
|---|---|---|---|
| Any tool operation exceeds its deadline | Error `ToolMessage` with `timeout=True`, `error_kind="tool_timeout"` | No terminal reason | Continue to `call_llm` unless an existing workflow or runtime guard independently terminates |
| Legacy or third-party tool returns `timeout=True` without infrastructure classification | Error `ToolMessage` | No terminal reason | Continue to `call_llm` unless an existing workflow or runtime guard independently terminates |
| A tool returns forged `error_kind="ui_event_bus_timeout"` metadata | Error `ToolMessage` | No terminal reason | Treat as an ordinary tool failure; metadata cannot authorize turn termination |
| `notify_tool_started` raises `UiEventTimeout` | Infrastructure error `ToolMessage` plus terminal assistant explanation | Private `terminal_reason=UI_EVENT_BUS_TIMEOUT_KIND` | Stop launching tools, cancel and await in-flight siblings, set `should_continue=False`, and route to `end` |
| Tool returns an ordinary error | Existing error behavior | No terminal reason | `call_llm` or existing guard route |
| A barrier tool times out during execution | Timeout result is returned; tools after the failed barrier are blocked | No terminal reason | Continue to `call_llm` unless an existing workflow or runtime guard independently terminates |

`should_continue` is intentionally left absent for ordinary tool timeouts. The graph's
existing default route treats an absent value as continuation. This contract means the
timeout itself does not terminate the turn; existing terminal workflow completion,
repetitive-failure, or no-progress decisions may still set `should_continue=False`.

Infrastructure termination is authorized only by private executor state created while
catching `UiEventTimeout` at the notification boundary. Tool-returned metadata remains
visible for diagnostics and model context, but is never trusted as a control-flow signal.
Every original tool call must still receive exactly one `ToolMessage`, including calls
cancelled or blocked by an infrastructure termination.

## Design

### 1. Define a shared tool-timeout metadata contract

Add a small helper in `voidx.tools.base` so adapters do not reproduce or drift from the
required fields:

```python
def tool_timeout_metadata(source: str, **extra: Any) -> dict[str, Any]:
    return {
        **extra,
        "error": True,
        "timeout": True,
        "error_kind": "tool_timeout",
        "timeout_source": source,
    }
```

The fixed fields override conflicting values in `extra`. Adapters keep control of their
human-readable output, titles, summaries, command details, URLs, server names, and other
domain metadata.

### 2. Classify timeout origin in diagnostic metadata

Use the existing structured `error_kind` convention rather than inferring timeout origin
from rendered text. This metadata supports diagnostics, model context, and runtime failure
classification; it does not grant control-flow authority.

Shell command timeouts use:

```python
metadata={
    "command": command,
    "exit_code": -1,
    "timeout": True,
    "error": True,
    "error_kind": "tool_timeout",
    "timeout_source": "shell",
}
```

UI event bus notification timeouts expose equivalent diagnostic metadata:

```python
metadata={
    "error": True,
    "timeout": True,
    "error_kind": "ui_event_bus_timeout",
    "timeout_source": "ui_event_bus",
}
```

The generic `timeout=True` field remains part of both results because it describes the
failure category and is already used by result-status and subagent logic. The executor's
private terminal field, not `error_kind`, determines whether the graph ends the turn.

### 3. Normalize timeout-capable adapters

Each adapter must classify a timeout only where the exception or structured process
result proves that a deadline was exceeded:

- **Shell:** continue using the shared Bash/PowerShell timeout builder and add the
  unified fields.
- **MCP:** split `McpTimeoutError` from connection/protocol errors in both
  `McpToolWrapper` and MCP-backed web delegation. MCP stdio spawn, handshake, and reconnect
  use the shared owned-process lifecycle; cancellation must clean reader/stderr tasks,
  pending requests, pipes, and the entire server process tree before propagating.
- **Git:** add a structured timeout marker to `_run_process` and preserve it through
  every process-result consumer. This includes repository discovery, `_git_raw`, and all
  structured handlers that call `_run_git`; no path may collapse a proven timeout into
  `not_a_git_repository` or a generic command failure. `_result` must receive the
  structured marker instead of parsing stderr text.
- **LSP:** add `LspTimeoutError` as a subtype of `LspConnectionError`; raise it from
  `LspClient.request` and catch it before the generic `LspError` branch in tool wrappers.
  LSP process creation, initialize request, initialized notification, and background
  reader/stderr tasks form one startup transaction that must be fully rolled back on
  cancellation or failure.
- **Direct Web:** catch `httpx.TimeoutException` after retry/fallback exhaustion. A
  timeout from the first search backend is not returned if a fallback succeeds.
- **Agent:** catch explicit `TimeoutError` from the child runner before the generic
  exception branch. The specification does not introduce a new child-agent deadline.

Unknown exceptions remain ordinary errors. Adapters must not inspect arbitrary error
strings to guess that a timeout occurred.

### 4. Represent infrastructure termination in trusted executor state

Extend the private `_ExecutedTool` record with executor-owned fields:

```python
@dataclass
class _ExecutedTool:
    message: ToolMessage | None
    result: object
    tool_call: dict
    todo_state: TodoRunState | None = None
    terminal_reason: str | None = None
    runtime_guard_eligible: bool = True
```

Define `UI_EVENT_BUS_TIMEOUT_KIND = "ui_event_bus_timeout"` in the tool executor. Only the
`execute_one` boundary that catches `UiEventTimeout` from `notify_tool_started` may set
`terminal_reason=UI_EVENT_BUS_TIMEOUT_KIND`. The corresponding result also includes the
diagnostic timeout metadata, but aggregation and routing inspect only `terminal_reason`.

The infrastructure result sets `runtime_guard_eligible=False` because the underlying tool
was not responsible for the failure. Every synthetic result created for a call cancelled,
blocked, or never started because of the same terminal event also sets
`runtime_guard_eligible=False`.

Catching must be specific to `UiEventTimeout`, not broad `TimeoutError`, so an unrelated
programming or adapter timeout cannot be promoted to an infrastructure termination.

### 5. Fail fast across concurrent groups and workflow barriers

The UI notification timeout remains a turn-level escape hatch, so detection must stop the
executor before it waits for unrelated tools or starts later phases.

`_execute_approved_batch` owns result completion within one approved batch and must
implement these rules:

1. Execute each concurrent group as tracked `asyncio.Task` objects rather than one opaque
   `gather` call.
2. As tasks complete, inspect their private `terminal_reason`.
3. On the first trusted terminal reason, cancel all unfinished sibling tasks in that
   group and await them with cancellation collected; do not leave detached tasks.
4. Preserve actual results for siblings that completed before terminal detection.
5. Produce exactly one synthetic infrastructure error `_ExecutedTool` for every sibling
   call cancelled before producing a result.
6. Do not start the non-file group when the file group produced a terminal reason; produce
   one synthetic infrastructure error `_ExecutedTool` for each call in that skipped group.
7. Preserve original tool-call ordering when returning actual and synthetic results.

The outer prefix/barrier/suffix loop owns calls outside the completed batch. Immediately
after each executed segment it must inspect trusted terminal state. If terminal state is
present, it must:

1. stop authorizing and executing subsequent segments;
2. create one synthetic infrastructure error `ToolMessage` for every original pending
   barrier or suffix call that will never enter `execute_approved`;
3. preserve original tool-call ordering across actual, denied, cancelled, skipped, and
   blocked messages; and
4. leave no original tool call without exactly one result message.

Synthetic messages must state that execution was skipped because tool-start notification
in the same turn timed out; they must not claim that the skipped tool itself timed out.
They use the same infrastructure diagnostic metadata, have no trusted terminal reason of
their own, and are excluded from runtime guards.

Cancellation is infrastructure-driven, not a tool failure. `execute_one` must retain its
existing `finally` cleanup for heartbeats and workspace locks. Every adapter or local service client that starts a child process and can be cancelled by
executor fail-fast behavior must use the neutral helpers in `voidx.runtime.processes`
rather than awaiting `asyncio.create_subprocess_*` directly. This includes Bash,
PowerShell, Git, LSP, and MCP stdio.

The helper creates a tracked task for process creation and awaits it through
`asyncio.shield`. If the caller is cancelled before creation resolves, the helper records
the cancellation but does not abandon or cancel the creation task. It continues awaiting
that task until exactly one ownership outcome is known:

1. creation fails before returning a process handle, in which case no process is owned; or
2. creation returns a process handle, in which case the helper immediately terminates and
   awaits the full process tree.

Only after ownership is resolved and any owned process tree has exited may the helper
re-raise the original `CancelledError`. Repeated cancellation during ownership resolution
or cleanup must be deferred rather than abandoning the tracked creation or termination
task. This safety-first wait is intentionally allowed to delay turn termination for the
short process-creation/cleanup window; returning earlier cannot guarantee leak freedom.

After successful creation has returned normally, cancellation while awaiting
`communicate()` follows the same rule: terminate and await the full process tree, then
re-raise `CancelledError`. Task cancellation is never converted into a `ToolResult` and
does not alter existing deadline-timeout result semantics.

Git must launch commands with process-tree isolation equivalent to the shell adapters:
use a new session/process group on Unix and a new process group or other tree-termination
compatible creation mode on Windows. Both deadline timeout and task cancellation must
terminate and await the entire Git process tree, including hooks, filters, credential
helpers, and other descendants—not only the direct `git` process. Prefer the shared
platform termination helper rather than maintaining divergent direct-process cleanup.

LSP and MCP stdio are long-lived local service processes and use the same tree-terminable
launch mode and ownership helper. Their startup is transactional beyond process creation:

- **LSP:** once a process handle is owned, initialization may create reader/stderr tasks and
  pending request futures. Cancellation or failure before `initialized` completes must
  cancel and await both background tasks, reject/clear pending requests, terminate and
  await the complete process tree, clear client fields, then re-raise the original
  cancellation or error. `LspManager._ensure_client` must catch `CancelledError`
  explicitly as a defensive boundary, await client cleanup through a shielded finalizer,
  and re-raise; it must not rely on `except Exception`.
- **MCP stdio:** spawn, pipe validation, reader/stderr task creation, and handshake are one
  startup transaction. `McpClient.start` and reconnect must catch `CancelledError`
  explicitly, run cancellation-resistant cleanup, and re-raise without wrapping it as
  `McpConnectionError`. Cleanup cancels and awaits background tasks, rejects pending
  requests, closes pipes, terminates and awaits the full server process tree, and clears
  all lifecycle fields. Background manager startup cancellation uses the same cleanup.

The shared helper owns process creation and tree finalization only; each client remains
responsible for protocol-specific tasks, futures, and fields. Cleanup/finalization must be
idempotent so manager-level defensive cleanup can safely run after client-level rollback.

Per-file locks must also be cancellation-safe. `execute_one_file_locked` must append a lock
to its acquired-lock list only after `acquire_read` or `acquire_write` succeeds, and release
only those acquired locks in reverse acquisition order. Cancellation while waiting for the
first lock releases nothing; cancellation after partially acquiring a multi-path lock set
releases exactly the acquired prefix. The implementation must not suppress
`asyncio.CancelledError` before all owned resources have been cleaned up.

A terminal prefix prevents authorization or execution of its
barrier and suffix. A terminal barrier prevents its suffix. No later segment may start.

### 6. Terminate only for the trusted UI event timeout

Replace the generic metadata scan:

```python
has_timeout = any(metadata.get("timeout") ...)
```

with a private execution-record check:

```python
has_ui_event_timeout = any(
    item.terminal_reason == UI_EVENT_BUS_TIMEOUT_KIND
    for item in executed
)
```

Only `has_ui_event_timeout` may append the frontend-unresponsive assistant message and
set `state_update["should_continue"] = False`. Tool metadata containing the same string
without the private terminal field must not terminate the turn.

The executor must compute this trusted terminal flag before the UI event-drain phase. When
it is true, `execute_tools` must not call the current unbounded
`host._ui.events.drain()`: the queued request that raised `UiEventTimeout` may still be
held by the stalled consumer, so `queue.join()` can never complete. The terminal path skips
drain entirely, emits a diagnostic log entry, and returns the graph state after tool/task
cleanup and result aggregation. It must not wait for, cancel, stop, or reset the shared UI
event-bus consumer as part of this feature. Non-terminal paths retain the existing drain
and error-clearing behavior.

Ordinary tool timeout results remain error results. They are included in returned tool
messages, but they do not add a terminal assistant message and do not change turn routing.

### 7. Isolate infrastructure failures from runtime guards

`_record_runtime_guard_outcomes` and `cycle_summary_from_tools` must receive only executed
records whose `runtime_guard_eligible` field is true. UI notification failures and
synthetic sibling-cancellation results must not:

- increment repeated tool-failure counts;
- mark the affected tool/arguments as a failed attempt;
- contribute to repetitive-tool or no-progress cycle summaries;
- affect the next turn after the UI event bus recovers.

Ordinary tool timeouts remain eligible and continue to participate in existing repeated
failure and no-progress behavior.

### 8. Preserve legacy and existing failure semantics

The executor must not terminate a turn merely because a tool result contains
`timeout=True`, even if that result has no `error_kind` or claims an infrastructure kind.
A missing or untrusted classification degrades to a normal tool failure.

No change is required to `_tool_result_ok`:

- `timeout=True` remains unsuccessful.
- The emitted `ToolMessage.status` remains `"error"`.
- `on-failure` permission handling still receives actual failed tool results.
- Runtime guards can classify repeated ordinary tool timeouts using
  `error_kind="tool_timeout"`.
- A failed barrier still prevents suffix tools from running.

The routing change is limited to whether trusted orchestration failure ends the turn;
adapter metadata alone cannot do so.

## Data Flow

### Ordinary tool timeout

```text
Any timeout-capable tool adapter
  -> operation exceeds configured timeout
  -> adapter performs its existing cleanup/retry exhaustion
  -> ToolResult(error=True, timeout=True, error_kind="tool_timeout", timeout_source=...)
  -> execute_one emits error ToolMessage with terminal_reason=None
  -> runtime guards record the ordinary tool failure normally
  -> execute_tools does not set should_continue=False because of the timeout
  -> route_after_execute_tools returns call_llm unless another guard terminates
  -> model reads timeout result and continues the turn
```

### Forged infrastructure metadata

```text
Registered tool returns error_kind="ui_event_bus_timeout"
  -> execute_one emits error ToolMessage with terminal_reason=None
  -> metadata remains available for diagnostics
  -> executor does not treat metadata as a terminal authorization
  -> route_after_execute_tools returns call_llm unless another guard terminates
```

### UI event bus timeout

```text
notify_tool_started
  -> UiEventBus.request stalls
  -> raises UiEventTimeout
  -> execute_one creates infrastructure result
       terminal_reason=UI_EVENT_BUS_TIMEOUT_KIND
       runtime_guard_eligible=False
  -> current concurrent group detects the trusted terminal reason
  -> unfinished sibling tasks are cancelled and awaited
  -> synthetic infrastructure messages complete cancelled tool calls
  -> later file/non-file groups, barriers, and suffixes are not started
  -> runtime guards ignore infrastructure and synthetic results
  -> execute_tools skips unbounded UI event-bus drain
  -> execute_tools appends terminal explanation
  -> execute_tools sets should_continue=False
  -> route_after_execute_tools returns end
```

## Files to Modify

| File | Change |
|---|---|
| `src/voidx/tools/base.py` | Add the shared `tool_timeout_metadata` helper |
| `src/voidx/runtime/processes.py` *(new)* | Add neutral shielded process creation, ownership resolution, tree-terminable launch options, and cancellation-resistant finalization helpers |
| `src/voidx/tools/shell/common.py` | Use/re-export the neutral process helpers and keep shell-specific timeout result construction |
| `src/voidx/tools/bash/tool.py` | On task cancellation, terminate and await the spawned Bash process, then re-raise cancellation |
| `src/voidx/tools/powershell/tool.py` | On task cancellation, terminate and await the spawned PowerShell process, then re-raise cancellation |
| `src/voidx/mcp/tool.py` | Split `McpTimeoutError` from generic MCP failures |
| `src/voidx/mcp/client/stdio_transport.py` | Route stdio spawn through the owned-process helper and make reader/stderr setup transactional |
| `src/voidx/mcp/client/base.py` | Roll back stdio startup/handshake/reconnect on cancellation, await background tasks, and use full-tree cleanup |
| `src/voidx/mcp/manager.py` | Ensure background startup cancellation awaits idempotent client cleanup |
| `src/voidx/tools/web/mcp.py` | Preserve MCP timeout classification for routed web tools |
| `src/voidx/tools/git.py` | Propagate structured timeout state, launch Git in a tree-terminable process group/session, and terminate/await the full process tree on deadline timeout or task cancellation |
| `src/voidx/lsp/errors.py` | Add `LspTimeoutError` |
| `src/voidx/lsp/client.py` | Use transactional owned-process startup; roll back initialization/background tasks on cancellation; raise `LspTimeoutError` for request deadlines |
| `src/voidx/lsp/manager.py` | Add an explicit cancellation cleanup boundary around client startup |
| `src/voidx/tools/lsp.py` | Return unified timeout metadata for LSP operations |
| `src/voidx/tools/web/fetch.py` | Return unified timeout metadata after fetch retries fail |
| `src/voidx/tools/web/search.py` | Return unified timeout metadata when the final search backend times out |
| `src/voidx/tools/agent.py` | Classify explicit child-runner timeout |
| `src/voidx/agent/graph/tool_executor/types.py` | Add trusted `terminal_reason` and `runtime_guard_eligible` fields |
| `src/voidx/agent/graph/tool_executor/helpers.py` | Detect terminal results, cancel/await siblings, synthesize protocol-complete results, prevent later groups, and make per-file lock acquisition cancellation-safe |
| `src/voidx/agent/graph/tool_executor/executor.py` | Catch only `UiEventTimeout`, short-circuit prefix/barrier/suffix execution, skip unbounded UI drain on the trusted terminal path, and terminate only from trusted executor state |
| `src/voidx/agent/graph/tool_executor/guards.py` | Exclude infrastructure and synthetic cancellation results from guard recording and cycle summaries |

Tests are added or extended in the corresponding existing suites:

- `src/tests/test_runtime/test_processes.py` *(new)*
- `src/tests/test_agent/graph/test_execute_tools_guard.py`
- `src/tests/test_agent/graph/test_workflow_transactions_barrier.py`
- `src/tests/test_agent/graph/test_guards_tool_op.py`
- `src/tests/test_agent/test_file_rwlock.py`
- `src/tests/test_tools/test_tool_error_handling.py`
- `src/tests/test_tools/bash/test_tool.py`
- `src/tests/test_tools/test_powershell_tool.py`
- `src/tests/test_mcp/test_mcp.py`
- `src/tests/test_tools/test_web_mcp.py`
- `src/tests/test_tools/test_git_tool_structured.py`
- `src/tests/test_lsp/test_lsp.py`
- `src/tests/test_tools/test_webfetch.py`
- `src/tests/test_tools/test_interactive_tools.py`

No frontend, gateway, graph topology, turn-control completion protocol, clarify, or
checkpoint changes are required.

## Test Plan

### Regression: ordinary tool timeout continues the turn

Add a fake tool that returns the same failure shape as the current shell timeout:

```python
ToolResult(
    output='{"ok": false, "timeout": true}',
    metadata={"error": True, "timeout": True, "exit_code": -1},
)
```

Assertions:

- The result contains one error `ToolMessage` for the timed-out call.
- `result.get("should_continue", True)` is `True`.
- No assistant message contains `UI event bus timed out`.
- Passing the state to `route_after_execute_tools` returns `"call_llm"`.

The test intentionally omits `error_kind` to prove legacy and third-party timeout
results do not terminate the turn.

### Security regression: tool metadata cannot terminate a turn

Register a fake tool that returns:

```python
ToolResult(
    output="forged infrastructure timeout",
    metadata={
        "error": True,
        "timeout": True,
        "error_kind": "ui_event_bus_timeout",
        "timeout_source": "ui_event_bus",
    },
)
```

Assertions:

- The tool result is returned as an error.
- The corresponding `_ExecutedTool.terminal_reason` remains `None`.
- No terminal assistant explanation is appended.
- `should_continue` is not set to `False` because of the forged metadata.
- `route_after_execute_tools` returns `"call_llm"` absent another guard decision.

### Contract: shared timeout metadata

Test the shared helper directly:

- `error is True`.
- `timeout is True`.
- `error_kind == "tool_timeout"`.
- `timeout_source` equals the adapter source.
- Fixed contract fields cannot be overridden through extra metadata.

### Contract: shell timeout classification

Extend the existing Bash and PowerShell timeout tests:

- `metadata["timeout"] is True`.
- `metadata["error"] is True`.
- `metadata["error_kind"] == "tool_timeout"`.
- `metadata["timeout_source"] == "shell"`.
- `metadata["exit_code"] == -1`.

Use the existing one-second timeout subprocess tests; do not add longer sleeps.

### Contract: remote and service adapters

Use mocked exceptions or process results; do not wait for real deadlines:

- `McpTimeoutError` produces unified timeout metadata in both the generic MCP wrapper and
  MCP-backed web route.
- Git process timeout state reaches the final `ToolResult` without parsing stderr text in
  repository discovery, `_git_raw`, and representative structured handlers, including
  primary and secondary `_run_git` calls.
- `LspClient.request` raises `LspTimeoutError`; `LspTool` converts it into a unified
  timeout result.
- Exhausted Web Fetch timeout retries produce a unified timeout result.
- Web Search returns a timeout result only when the final backend fails by timeout; a
  successful fallback remains successful.
- An explicit child-runner `TimeoutError` produces a unified Agent timeout result.

### Preserve: trusted UI notification timeout terminates the turn

Test the result builder/private execution record and graph behavior separately.

Direct assertions:

- Diagnostic metadata contains `timeout=True`, `error=True`,
  `error_kind=UI_EVENT_BUS_TIMEOUT_KIND`, and `timeout_source="ui_event_bus"`.
- `terminal_reason == UI_EVENT_BUS_TIMEOUT_KIND`.
- `runtime_guard_eligible is False`.

Extend the existing graph test where `notify_tool_started` raises `UiEventTimeout`:

- The underlying tool body is not executed.
- `should_continue is False`.
- The infrastructure tool result is an error.
- The terminal assistant explanation remains present.
- `route_after_execute_tools` returns `"end"`.

Add a separate test proving a generic tool or adapter `TimeoutError` cannot enter this
path unless it is the specific `UiEventTimeout` raised by `notify_tool_started`.

Add a real event-bus stall regression rather than relying only on a mocked exception:

- Start a real `UiEventBus` with a consumer whose async `handle` blocks on an event.
- Wrap the real `request` method only to use a deterministic short timeout/retry count;
  the request must still enter the real queue and be owned by the blocked consumer.
- Execute a graph tool call and assert `execute_tools` returns within a bounded test timeout
  after `UiEventTimeout`, with `should_continue=False` and route `"end"`.
- Assert the executor did not call or await the unbounded `drain()` path, no tool sibling
  task remains pending, and every tool call still has exactly one result.
- Release the consumer in test cleanup, then stop the event bus so the test itself leaves
  no background queue task.

### Concurrency: fail fast and clean up in-flight siblings

Add deterministic async tests using events rather than sleeps:

1. **File group terminal event**
   - One file-group call returns the trusted UI terminal result.
   - A sibling file call is already running and records cancellation/finally cleanup.
   - The sibling is cancelled and awaited.
   - No non-file call starts.
   - Every original call receives exactly one `ToolMessage` in original order.

2. **Prefix terminal event**
   - A prefix contains a trusted UI terminal result.
   - The following barrier and suffix tools are never authorized or executed.
   - The graph routes to `end`.

3. **Barrier terminal event**
   - `notify_tool_started` for the barrier raises `UiEventTimeout`.
   - Its suffix is never executed.
   - The graph routes to `end`.

The tests must also assert that no async task remains pending after executor return.

### Cancellation cleanup: processes, local services, and file locks

Add deterministic cancellation tests; do not rely on process completion races.

1. **Shared owned-process lifecycle**
   - Test post-creation cancellation, creation-pending ownership resolution, creation
     failure before a handle exists, and repeated cancellation during ownership resolution
     or tree finalization.
   - Use a controlled creation coroutine that owns a mock process but pauses before
     returning its handle. Cancellation must not return early; once the handle is released,
     the full tree is terminated and awaited before `CancelledError` propagates.
   - Verify platform launch options establish a tree-terminable process group/session and
     finalization is idempotent.

2. **Bash and PowerShell cancellation**
   - Route process creation through the shared helper.
   - Cancel after creation and during the controlled creation race.
   - Assert process-tree exit is awaited and no timeout `ToolResult` is returned for task
     cancellation.

3. **Git process-tree timeout and cancellation**
   - Assert Git uses the shared helper and platform tree-termination options.
   - Use a long-lived descendant to prove both deadline timeout and task cancellation
     terminate and await the direct process plus descendants.
   - Cover both creation-race ownership outcomes.

4. **LSP startup and initialization cancellation**
   - Cancel while process creation owns a process but has not returned its handle.
   - Cancel after process creation while the initialize response is pending, and again
     while the initialized notification or final startup step is pending.
   - Assert reader/stderr tasks are cancelled and awaited, pending futures are rejected and
     cleared, the full process tree exits, client fields are reset, and cancellation is
     re-raised rather than wrapped.
   - Cover process creation failure, initialization failure, repeated cancellation during
     cleanup, and safe idempotent manager-level cleanup.

5. **MCP stdio spawn, handshake, and reconnect cancellation**
   - Cancel during the controlled spawn race and after spawn while handshake is pending.
   - Exercise the same path through on-demand reconnect from a tool call and through
     background manager startup cancellation.
   - Assert read/stderr tasks are cancelled and awaited, pending requests are rejected,
     pipes and lifecycle fields are cleared, the entire server process tree exits, and
     `CancelledError` is re-raised without becoming `McpConnectionError`.
   - Cover spawn failure before a handle exists and repeated cancellation during cleanup.

6. **File-lock cancellation before acquisition**
   - Hold a writer lock, start a waiting reader, then cancel the reader.
   - Hold a reader lock, start a waiting writer, then cancel the writer.
   - Assert lock counters/state remain unchanged and the original owner still holds the
     lock until its own release.

7. **Partial multi-path acquisition cancellation**
   - Let a tool acquire the first sorted path lock and block on the second.
   - Cancel the tool task.
   - Assert only the first lock is released, release order is reversed, and both locks
     remain usable afterward.

Shared-helper tests live in `src/tests/test_runtime/test_processes.py`. Adapter/client tests
extend the existing Bash, PowerShell, Git, LSP, and MCP suites; file-lock tests remain in
`src/tests/test_agent/test_file_rwlock.py`.

### Protocol completeness: mixed restoration paths

Add a single batch regression that combines all restoration layers affected by terminal
short-circuiting:

- two identical read calls so one result must be restored by duplicate-read handling;
- one call pre-blocked by the runtime guard;
- one call denied during authorization;
- one running call whose `notify_tool_started` raises `UiEventTimeout`;
- one in-flight sibling cancelled by the terminal event; and
- one later file/non-file group, barrier, or suffix call that never starts.

Assert that every original tool-call id appears exactly once, no unexpected id appears,
and final `ToolMessage` order matches the original model call order. Verify the duplicate,
guard-blocked, denied, terminal, cancelled, and never-started messages retain their own
correct reason instead of being overwritten by a generic terminal result. Also assert no
restoration iterator underflows/overflows and no async task remains pending.

### Runtime guards: infrastructure failures are isolated

Exercise `_record_runtime_guard_outcomes` with a trusted UI timeout and synthetic sibling
cancellation results:

- Tool-failure counters do not change.
- Repetitive-tool and no-progress cycle summaries contain no infrastructure-only tools.
- No guard guidance is emitted for those results.
- After simulating UI recovery, the same tool call can execute without inheriting a
  repeated-failure block from the infrastructure event.

A separate ordinary `tool_timeout` test must confirm real tool timeouts remain guard
eligible.

### Barrier: ordinary timeout blocks suffix but continues the turn

This is a mandatory regression test, not conditional on existing ordinary-failure
coverage:

- A barrier tool returns `ToolResult` with `error=True`, `timeout=True`, and
  `error_kind="tool_timeout"`.
- Tools after the barrier are not executed.
- The barrier result is an error `ToolMessage`.
- No UI terminal explanation is appended.
- `should_continue` is not set to `False` because of the timeout.
- `route_after_execute_tools` returns `"call_llm"` absent an independent guard decision.

## Verification

Run focused tests first:

```bash
./test.py --backend -- \
  src/tests/test_runtime/test_processes.py \
  src/tests/test_agent/graph/test_execute_tools_guard.py \
  src/tests/test_agent/graph/test_workflow_transactions_barrier.py \
  src/tests/test_agent/graph/test_guards_tool_op.py \
  src/tests/test_agent/test_file_rwlock.py \
  src/tests/test_tools/test_tool_error_handling.py \
  src/tests/test_tools/bash/test_tool.py \
  src/tests/test_tools/test_powershell_tool.py \
  src/tests/test_mcp/test_mcp.py \
  src/tests/test_tools/test_web_mcp.py \
  src/tests/test_tools/test_git_tool_structured.py \
  src/tests/test_lsp/test_lsp.py \
  src/tests/test_tools/test_webfetch.py \
  src/tests/test_tools/test_interactive_tools.py -v
```

The focused command includes the existing Web Search coverage in
`test_web_mcp.py` and Agent runner coverage in `test_interactive_tools.py`. Then run the
backend suite:

```bash
./test.py --backend
```

The implementation is complete when:

- Ordinary and legacy tool timeouts return to the LLM unless an independent guard ends
  the turn.
- Tool-returned metadata cannot forge infrastructure termination.
- A trusted UI notification timeout cancels and awaits in-flight siblings, prevents all
  later groups/barriers/suffixes from starting, completes every tool-call protocol entry
  exactly once, and returns without waiting on the stalled UI queue's unbounded drain.
- Cancelled Bash and PowerShell tasks terminate and await their process trees before
  propagating `CancelledError`.
- Git uses platform-appropriate process-tree isolation and terminates/awaits the full tree
  on both deadline timeout and task cancellation.
- LSP startup cancellation or failure rolls back initialization, background tasks, pending
  requests, client fields, and the complete server process tree before propagating.
- MCP stdio spawn, handshake, reconnect, and background startup cancellation roll back
  tasks, pending requests, pipes, lifecycle fields, and the complete server process tree.
- Cancellation during process creation is deferred until the tracked creation task
  resolves ownership; if a handle appears, the complete process tree is terminated and
  awaited before cancellation propagates, while a true no-process outcome performs no
  termination.
- Cancelling a file tool while it waits for locks cannot release an unowned lock; partial
  multi-path acquisition releases only acquired locks in reverse order.
- Mixed duplicate-read, guard-blocked, denied, terminal, cancelled, and never-started calls
  each produce exactly one correctly classified `ToolMessage` in original order.
- UI infrastructure and synthetic cancellation results do not affect runtime guards.
- Ordinary tool timeouts remain visible to runtime guards.
- A timed-out barrier blocks its suffix but returns control to the LLM.
- All adapters that can identify timeout return the unified metadata contract.
- Git preserves classified timeouts through repository discovery, raw commands, and
  structured handlers.
- Bash and PowerShell process cleanup tests still pass.
- No focused or backend regression tests fail.

## Risks

1. **Async cancellation correctness**  
   Cancelling siblings can leak tasks, heartbeats, locks, or subprocess trees if cleanup
   does not run. Track tasks explicitly, shield process creation from caller cancellation,
   resolve ownership before returning, terminate and await owned process trees, and verify
   post-creation, creation-pending, creation-failure, and repeated-cancellation paths with
   deterministic tests.

2. **Lock ownership corruption**  
   Releasing a lock whose acquisition was cancelled can decrement reader counts below zero
   or clear another task's writer state. Track only successfully acquired locks and release
   them in reverse order; cover waiting-reader, waiting-writer, and partial multi-path
   cancellation.

3. **Protocol completeness after short-circuit**  
   The provider protocol requires one result for each original tool call. Generate
   synthetic infrastructure errors for cancelled or unstarted calls and preserve original
   ordering.

4. **Broader adapter surface**  
   Standardizing MCP, Git, LSP, Web, and Agent touches more files than the routing-only
   fix. Implement adapter changes in isolated phases with focused tests before the full
   suite.

5. **Repeated model retries after ordinary timeout**  
   The model may retry the same long-running command. Existing repetitive-tool and
   no-progress guards remain responsible for stopping unproductive cycles.

6. **Accidental trust of adapter metadata**  
   Future refactors could reintroduce metadata-based termination. The forged-metadata
   regression test and private `_ExecutedTool.terminal_reason` invariant prevent this.

7. **Barrier semantics divergence**  
   An ordinary timed-out barrier blocks dependent tools but continues the turn; an
   infrastructure timeout stops the entire executor. Separate tests must preserve both
   behaviors.

8. **Long-lived service rollback complexity**  
   LSP and MCP stdio startup spans process creation, protocol initialization, background
   tasks, and pending futures. Treat startup as an idempotent transaction and test
   cancellation at every boundary so partial clients cannot survive or be reused.

9. **Terminal UI drain deadlock**  
   A timed-out request can remain owned by a blocked event-bus consumer, so the queue's
   unfinished-task count never reaches zero. Detect trusted terminal state before drain and
   skip drain on that path; verify with a real blocked consumer rather than only a mocked
   `UiEventTimeout`.

## Decisions

| Decision | Alternative | Rationale |
|---|---|---|
| Use private `_ExecutedTool.terminal_reason` to authorize termination | Route directly from `ToolResult.metadata` | Tool and adapter metadata is untrusted and dynamically extensible; only the executor knows the exception origin |
| Keep `error_kind` and `timeout_source` as diagnostic metadata | Remove infrastructure metadata entirely | Logs, model context, and support diagnostics still need a structured failure description |
| Use one ordinary `tool_timeout` kind plus `timeout_source` | Create `mcp_timeout`, `git_timeout`, `lsp_timeout`, etc. | Runtime behavior is shared; source remains available without fragmenting guard classification |
| Cancel and await in-flight siblings after trusted terminal detection | Wait for the entire group or leave tasks detached | The UI timeout is an escape hatch; waiting can preserve the hang, while detaching leaks work |
| Skip UI event-bus drain after a trusted UI notification timeout | Always call `drain()` or add another long drain timeout | The timed-out request may still be owned by the blocked consumer, making `queue.join()` unbounded; the terminal path must return without depending on consumer recovery |
| Put shared process ownership in neutral `voidx.runtime.processes` helpers used by shell, Git, LSP, and MCP stdio | Keep helpers under shell or duplicate lifecycle code | Process ownership is infrastructure behavior shared by tools and local service clients; a neutral module prevents dependency inversion and cleanup drift |
| Shield and track process creation until ownership is resolved, then terminate/await any owned tree before re-raising cancellation | Propagate cancellation immediately or kill only a known direct process | Immediate propagation can orphan a process created before its handle reaches the caller; direct-process kill can leak descendants |
| Treat LSP and MCP stdio startup as rollback-capable transactions | Clean only the subprocess | Initialization also owns background tasks, pending futures, pipes, and client fields that must not survive cancellation |
| Release only successfully acquired file locks in reverse order | Release every lock whose acquisition was attempted | Cancellation can occur while waiting; releasing an unowned lock corrupts shared lock state |
| Generate synthetic results for cancelled/unstarted calls | Return only completed results | Maintains one `ToolMessage` per original call and preserves provider protocol validity |
| Exclude infrastructure results from runtime guards | Record them as tool failures | The underlying tool did not fail and must not poison later failure/no-progress decisions |
| Normalize every adapter with an explicit timeout signal | Only fix Bash/PowerShell | Gives the model, logs, and guards a consistent contract across tools |
| Keep generic `timeout=True` | Remove it from UI timeout results | Existing result-status and diagnostic code recognizes timeout as a failure category |
| Continue after ordinary tool timeout | End the turn after any timeout | Tool failure is actionable context for the model and does not imply orchestration failure |
| Do not auto-retry | Retry once with a larger timeout | Retry policy is task-specific and should remain under model/runtime-guard control |

## Implementation Order

1. Add failing graph tests for legacy timeout continuation and forged infrastructure
   metadata.
2. Add failing executor tests for a real blocked UI-event consumer, file-group
   cancellation, prefix/barrier short-circuit, mixed protocol completeness, and guard
   isolation.
3. Add the shared `tool_timeout_metadata` helper and its unit test.
4. Extend `_ExecutedTool` with trusted terminal and guard-eligibility fields.
5. Catch only `UiEventTimeout` at `notify_tool_started` and create the trusted
   infrastructure result.
6. Make file-lock acquisition cancellation-safe and add waiting-reader, waiting-writer,
   and partial multi-path acquisition tests.
7. Add `voidx.runtime.processes` with failing-then-passing tests for shielded creation,
   ownership resolution, process-tree launch/finalization, creation failure, idempotence,
   and repeated cancellation.
8. Route Bash and PowerShell through the shared process lifecycle and verify cancellation
   cleanup plus ordinary timeout behavior.
9. Route Git through the shared lifecycle, preserve structured timeout state, and test
   repository discovery, raw/structured handlers, descendants, creation races, timeout,
   and cancellation.
10. Make LSP startup transactional: shared process ownership, explicit cancellation
    rollback, awaited reader/stderr cleanup, pending-request cleanup, and idempotent manager
    defense; add startup-boundary tests.
11. Make MCP stdio spawn/handshake/reconnect transactional with the shared lifecycle and
    cancellation-resistant cleanup; add tool-call reconnect and background startup tests.
12. Implement tracked concurrent-task cancellation, synthetic results, and file/non-file
    group short-circuiting.
13. Add mixed restoration-path coverage for duplicate reads, runtime-guard blocks,
    authorization denials, terminal results, cancelled siblings, and unstarted calls.
14. Short-circuit the outer prefix/barrier/suffix loop, compute trusted terminal state
    before UI drain, skip drain on that terminal path, and route only from trusted state.
15. Filter infrastructure results out of runtime guard recording and cycle summaries.
16. Add the mandatory ordinary barrier-timeout continuation regression.
17. Normalize timeout metadata for Shell, MCP wrappers, MCP-backed Web, LSP, direct Web,
    and Agent with their focused tests.
18. Run the complete focused timeout matrix, then `./test.py --backend`.
