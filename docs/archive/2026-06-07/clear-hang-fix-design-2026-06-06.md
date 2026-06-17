# `/clear` Hang Fix Design

> **Status: Done**

Date: 2026-06-06

## Problem

`/clear` should feel immediate: it clears the visible conversation, abandons the
current turn, and starts a fresh context. Today it can look stuck in two cases:

1. The agent is still running. TUI input is serialized through one submit queue,
   so `/clear` waits behind the current LLM turn with no visible effect.
2. The command resets the dock directly and then re-renders startup through
   `ui_events.request()`. That request waits for the UI event consumer to finish
   rendering. If the consumer path stalls during `refresh()` / TUI invalidation,
   the future never resolves and `/clear` remains busy.

The fix must prioritize foreground responsiveness. Persistence cleanup is
important, but it should not block clearing the screen.

## Root Cause

### 1. Busy Submit Queue

`PureTui._consume()` runs one submitted item at a time:

```python
self._current_submit_task = asyncio.create_task(on_submit(submit_text))
keep_running = await self._current_submit_task
```

If the user enters `/clear` while an LLM turn is active, the command is queued
behind that turn. Nothing cancels the active submit task, so the user sees a
hang even though the queue is behaving as designed.

`/guide` already has a busy bypass in `PureTui._do_submit()`. `/clear` needs a
different busy path: cancel the active submit task, discard queued work, and
enqueue `/clear` as the next command.

### 2. Startup Re-render Waits on Event Bus

The pre-fix `SlashSessionMixin._clear()` implementation reset the dock directly,
then called `_show_startup()`:

Pre-fix code:

```python
active_dock.reset()
await self._show_startup()
```

When `ui_events` is running, `_show_startup()` uses `ui_events.request()` for
`StartupShown`:

```python
await ui_events.request(startup_event)
```

`request()` waits for the consumer to process the event. For `/clear`, that is
unnecessary because the dock was just reset and startup can be rendered directly
without ordering against prior events.

### 3. Reusing the Same Session Conflicts With Background Cleanup

If `/clear` schedules `clear_messages(current_session_id)` in the background
while continuing to use the same session id, the next user turn can save new
messages before the background delete runs. The delete would then remove new
post-clear messages.

Therefore `/clear` must not reuse the old session id. It should detach from the
old session immediately. The next real user turn can create a new session id
through the normal session creation path.

## Design

### 1. Busy `/clear` Cancels the Active Turn

In `PureTui._do_submit()`:

1. If `_busy` and the stripped input is exactly `/clear`:
   - record history and clear the input;
   - drain pending submit queue items so stale user prompts do not run after
     clear;
   - set `_submit_cancel_requested = True`;
   - cancel `_current_submit_task` if it is still running;
   - enqueue `/clear` as the next submit item;
   - set a notice such as `Clearing current turn...`.
2. Let the existing submit loop continue. Once the active task observes
   cancellation, `_consume()` processes `/clear`.

`GraphRunLoopMixin._handle_user_input()` already catches `CancelledError` from
`_run_once()`, and `_run_once()` already deletes the pending user message on
cancel. This path should be reused rather than adding a separate LLM cancel
mechanism.

### 2. `/clear` Renders First, SQLite Cleanup Runs in Background

`VoidXGraph.clear_current_session()` becomes a fast in-memory operation:

1. Capture `old_session_id = self._session.id` if a session exists.
2. Detach from the old session:
   - `self._session = None`
   - reset `_session_msg_cache`, `_context_cache`, `_session_date`
   - reset runtime state, task state, summaries, permissions, todos, usage
3. Schedule background cleanup for the old session id:
   - `clear_messages(old_session_id)`
   - `update_title(old_session_id, "New session", touch=False)`
4. Return without awaiting SQLite cleanup.

The next user turn will create a fresh session id. This avoids any race where
background cleanup deletes new post-clear messages.

Background cleanup must catch exceptions and display a visible error. It should
also track tasks in a set to avoid losing task references. The title update must
not bump `updated_at`; otherwise a delayed cleanup can make the old empty
session appear newer than the fresh post-clear session.

### 3. Startup Uses Direct Rendering for Clear

Add a `prefer_direct: bool = False` parameter to `_show_startup()` and the
public `show_startup()` wrapper. When `prefer_direct=True`, skip
`ui_events.request()` and render directly to the active dock:

```python
startup_via_event = active_dock is not None and ui_events.is_running and not prefer_direct
```

`SlashSessionMixin._clear()` should call:

```python
await self._show_startup(prefer_direct=True)
```

This keeps `/clear` out of the event-bus request/future path.

## What Does Not Change

- No global `ui_events.request()` direct fallback. A timed-out queued request can
  still be processed later, so direct fallback risks duplicate rendering.
- No `skip_transcript` parameter. `/clear` calls `_show_startup()` with
  `append_transcript=False`, so transcript restoration is already skipped.
- No concurrent execution of arbitrary submit queue items. Only busy `/clear`
  gets the cancel-and-run-next behavior.
- `/guide` does not get cancel-and-run-next behavior. Guidance is additive: it
  injects extra instructions into the running turn when the next LLM call
  happens. It should not replace the current turn, drain queued user prompts, or
  force a new command to run after cancellation. The busy `/guide` path bypasses
  the submit queue and sends guidance directly to the running graph.

## Implemented Changes

### 1. Startup Direct Mode

Files:

- `src/voidx/agent/graph/run_loop.py`
- `src/voidx/agent/graph/core.py`
- `src/voidx/agent/graph/contracts.py`
- `src/voidx/agent/slash/session.py`

Changes:

- Added `prefer_direct` to `_show_startup()` and the graph wrapper.
- Forwarded `prefer_direct` through the slash/session protocol.
- `/clear` now calls `_show_startup(prefer_direct=True)`, so startup redraw does
  not wait on `ui_events.request()`.

### 2. Non-Reused Session Clear

Files:

- `src/voidx/agent/graph/core.py`
- `src/voidx/agent/slash/session.py`

Changes:

- `clear_current_session()` now detaches from the old session immediately and
  clears in-memory runtime state.
- Old-session SQLite cleanup is scheduled as a tracked background task.
- The old session title is reset with `touch=False`, so delayed cleanup does not
  make the old empty session appear newer than the fresh session.

### 3. Busy `/clear` TUI Path

File:

- `src/voidx/ui/tui/app.py`

Changes:

- Busy `/clear` drains pending submit queue items, enqueues `/clear`, cancels
  the active submit task, and shows `Clearing current turn...`.
- Busy `/guide` remains a direct guidance bypass and does not cancel the current
  submit task.

## Tests

| Test | File | Coverage |
|------|------|----------|
| `test_tui_busy_clear_cancels_current_submit_and_runs_clear_next` | `tests/test_pure_tui.py` | Busy `/clear` cancels the active submit task, drops stale queued input, and runs `/clear` next. |
| `test_tui_busy_guide_bypasses_submit_queue` | `tests/test_pure_tui.py` | Busy `/guide` bypasses the submit queue without canceling/replacing the running turn. |
| `test_clear_reprints_startup` | `tests/test_agent/test_run_loop.py` | `/clear` resets UI and graph runtime state, redraws startup, and does not restore transcript snapshot. |
| `test_clear_detaches_old_session_and_cleans_storage_in_background` | `tests/test_agent/test_run_loop.py` | `/clear` detaches from the old session, background cleanup clears old messages/title, and post-clear messages in a new session survive. |
| `test_show_startup_prefer_direct_skips_event_request` | `tests/test_agent/test_run_loop.py` | `_show_startup(prefer_direct=True)` skips `ui_events.request()` and renders directly. |
| `test_smart_title_does_not_update_after_clear` | `tests/test_agent/test_run_loop.py` | Delayed title generation cannot overwrite the cleared old session after `/clear`. |

## Acceptance Criteria

- Typing `/clear` during an active LLM turn cancels that turn and runs clear
  next.
- `/clear` visibly clears the dock and redraws startup before SQLite cleanup
  work is awaited.
- Post-clear user turns use a new session id.
- Background cleanup cannot delete post-clear messages.
- Startup rendering for `/clear` does not call `ui_events.request()`.
