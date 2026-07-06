# Tool Execution UI Event Disconnect Fix

> **Status: Done**

## Summary

This change fixes a failure mode where tool execution could appear to hang and then disconnect before the tool actually started, especially when UI event delivery was slow or blocked. It also adds long-running tool heartbeats and isolates recoverable UI drain/rendering failures from tool-result propagation.

## Problem

Users observed that tool calls did not merely return an error: the turn could get stuck and then disconnect. The failures were strongly correlated with tool execution timing and UI event delivery around tool start.

Representative log symptoms:

- `Turn terminated: UI event bus timed out while notifying tool start`
- `ui_event_bus_request_timeout ... TurnStarted`
- Tool-start events timing out before the actual tool process had a chance to run.

## Root Cause

### Blocking Gateway Mirror

`ToolStarted` and `TurnStarted` are sent through `ui_events.request()`, which requires event consumers to finish before the request completes.

`CompositeEventConsumer.handle()` awaited both the primary dock consumer and gateway mirror consumers. If a gateway mirror, websocket, or downstream UI path became slow or stuck, the whole request blocked. Because tool-start notification happened before the tool process began, the tool could never start, and the turn eventually timed out or disconnected.

### Silent Long-Running Tools

Some long-running tools, especially shell-style tools awaiting process completion, can remain silent while running. Without an intermediate progress signal, the UI and caller cannot easily distinguish a healthy long-running command from a stuck turn.

### Drain/Render Failures Escalating Too Far

Tool execution could complete, but a later UI event drain or result-rendering failure could still bubble into the execution path. That made a recoverable presentation-layer issue look like a tool execution failure.

## Design

The fix follows three rules:

1. The primary UI path should not depend on gateway mirror completion.
2. Long-running tools should emit lightweight periodic heartbeats.
3. Recoverable UI drain/render failures should be logged and isolated from tool-result propagation.

## Implementation

### `src/voidx/ui/output/events/consumers.py`

`CompositeEventConsumer.handle()` now awaits the primary dock consumer and schedules mirror consumers in background tasks.

Behavior:

- Dock updates remain synchronous and authoritative.
- Gateway mirror delivery no longer blocks `ui_events.request()`.
- Synchronous mirror scheduling failures are logged.
- Asynchronous mirror task failures are logged through a completion callback.

This directly addresses the hard hang where `ToolStarted` or `TurnStarted` could time out while waiting on a slow gateway/websocket mirror.

### `src/voidx/agent/graph/tool_executor/executor.py`

Tool execution now emits heartbeats for tools that remain active beyond the initial threshold.

Constants added:

- `TOOL_HEARTBEAT_INITIAL_SECONDS = 15.0`
- `TOOL_HEARTBEAT_INTERVAL_SECONDS = 15.0`

Heartbeat behavior:

- After 15 seconds, emit `StatusUpdated`.
- Continue emitting every 15 seconds while the tool is still running.
- Use `status_id=f"tool-heartbeat:{tool_event_id}"`.
- Use `display="record_only"` so the heartbeat is available to the event stream without forcing noisy UI display.
- On completion or cancellation, emit `StatusFinished(..., remove=True)`.

The executor also wraps final event bus `drain()` in `try/except`, logs `ui_event_drain_failed`, and clears the recoverable bus error instead of crashing tool execution after the result exists.

### `src/voidx/ui/output/events/bus.py`

Added `clear_error()` to reset the bus `_last_error` after a recoverable drain failure has been logged and handled.

This prevents one recoverable UI delivery problem from poisoning subsequent tool execution flow.

## Tests

Updated and added focused regression coverage:

- `src/tests/test_ui/gateway/test_ui_gateway.py`
  - `test_composite_event_consumer_handle_does_not_wait_for_slow_mirror`
  - Updated `test_composite_event_consumer_keeps_dock_primary_and_mirrors_events` to account for async mirror delivery.
- `src/tests/test_agent/graph/test_execute_tools_guard.py`
  - `test_execute_tools_returns_tool_error_when_result_rendering_fails`
  - `test_execute_tools_emits_heartbeat_while_tool_is_still_running`

Focused verification command:

```bash
./python.sh -m pytest \
  src/tests/test_ui/gateway/test_ui_gateway.py \
  src/tests/test_ui/gateway/test_ui_events_dock_bus.py \
  src/tests/test_ui/gateway/test_adapter.py \
  src/tests/test_agent/graph/test_execute_tools_guard.py \
  -q
```

Result:

```text
67 passed in 1.67s
```

## Expected Behavior After Fix

- A slow or stuck gateway mirror should not prevent tool execution from starting.
- Long-running tools should periodically record heartbeat status while they are still running.
- Tool results should still propagate when UI rendering or final event drain encounters a recoverable failure.
- Recoverable UI bus errors should be logged with enough detail for diagnosis, then cleared once handled.

## Follow-Up

One remaining area worth monitoring is large one-shot tool output flushing. If websocket queue pressure or very large terminal output still causes visible delay, consider adding bounded output chunking, backpressure metrics, or mirror-side queue limits.
