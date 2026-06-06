# `/clear` Hang Fix Design

> **Status: Draft**

## Problem

Users report that `/clear` sometimes hangs — the command is entered but the UI becomes unresponsive. Root cause analysis identified three contributing factors, with the event bus deadlock being the primary culprit.

## Root Cause Analysis

### 1. 🔴 Event Bus `request()` Deadlock (Primary)

`/clear` calls `_show_startup()`, which takes the event bus path when `via_events()` is true:

```python
# run_loop.py:52-53
if startup_via_event:
    await ui_events.request(startup_event)  # ← awaits future
```

`ui_events.request()` puts a `StartupShown` event into the bus queue and **blocks on a future** until the consumer processes it. The consumer runs as an `asyncio.Task` in the same event loop.

The deadlock scenario:

1. `_consume()` picks up `/clear` from the input queue
2. It `await`s the `on_submit` callback (which runs `_clear`)
3. Inside `_clear`, `_show_startup()` calls `ui_events.request()`
4. `request()` puts the event and awaits the future
5. The consumer task needs event loop time to process the event and resolve the future
6. **But the event loop is occupied by the `await` chain from step 2**

In practice, `await` yields control back to the event loop, so this shouldn't deadlock in theory. However, the consumer's `handle()` calls `append_startup()` → `refresh()` → `Live.update()`, and if `Live` is in a state where it's waiting for a render cycle or holding a lock, the consumer task can't complete, and the future never resolves.

Additionally, `_consume` sets `self._busy = True` before calling `on_submit`, and the TUI's `invalidate()` (called by `refresh()`) checks `_flush_committed()` which has busy-state logic. This creates a subtle interaction where the dock's refresh during a busy state may not behave as expected.

### 2. 🟡 SQLite Lock Contention

`clear_current_session()` performs multiple sequential `_write_transaction` calls:

- `clear_messages()` — DELETE from 7 tables (1 transaction)
- `update_title()` — UPDATE (1 transaction)
- `_clear_runtime_state()` — DELETE from 3 tables (1 transaction)

After these deletes, `_restore_transcript_snapshot()` calls `load_transcript()`, which reads from `transcript_nodes` — a table that was just deleted from. If the write lock hasn't fully released, the read blocks.

### 3. 🟡 Serial Input Queue Blocks `/clear` During Agent Execution

`_consume` processes input serially:

```python
# app.py:436-437
self._current_submit_task = asyncio.create_task(on_submit(item))
keep_running = await self._current_submit_task  # blocks until done
```

If the agent is mid-turn (`_run_once` is running), a queued `/clear` won't execute until the current turn finishes. The user sees no feedback — the command appears to hang.

## Design

### Fix 1: Avoid `ui_events.request()` in `_show_startup` During Clear

**Problem:** `request()` blocks on a future, creating a deadlock risk.

**Solution:** When called from `/clear`, use `emit_nowait()` or fall through to the direct `append_startup()` path instead of `request()`.

Add a parameter to `_show_startup` to control the rendering strategy:

```python
async def _show_startup(
    self: GraphRunLoopHost,
    *,
    append_transcript: bool = False,
    prefer_direct: bool = False,  # NEW: skip request(), use direct path
) -> None:
    ...
    startup_via_event = (
        active_dock is not None
        and ui_events.is_running
        and not prefer_direct  # NEW
    )
    if startup_via_event:
        await ui_events.request(startup_event)
        ...
        return

    # Direct path — no future, no deadlock risk
    if active_dock is not None and active_dock.active:
        active_dock.append_startup(...)
        ...
```

In `_clear()`, call `_show_startup(prefer_direct=True)`.

**Trade-off:** The startup banner may appear slightly less synchronized with the event stream, but since we just cleared everything, visual ordering is not a concern.

### Fix 2: Skip `_restore_transcript_snapshot` After Clear

**Problem:** After `clear_messages()` deletes all transcript data, `_restore_transcript_snapshot` reads from the DB only to find nothing.

**Solution:** Add a parameter to `_show_startup` to skip transcript restoration:

```python
async def _show_startup(
    self: GraphRunLoopHost,
    *,
    append_transcript: bool = False,
    prefer_direct: bool = False,
    skip_transcript: bool = False,  # NEW
) -> None:
    ...
    if startup_via_event:
        await ui_events.request(startup_event)
        if append_transcript and not skip_transcript:
            await self._restore_transcript_snapshot(append=True)
        return

    if active_dock is not None and active_dock.active:
        active_dock.append_startup(...)
        if append_transcript and not skip_transcript:
            await self._restore_transcript_snapshot(append=True)
        return
```

In `_clear()`, call `_show_startup(prefer_direct=True, skip_transcript=True)`.

### Fix 3: Add Timeout to `ui_events.request()`

**Problem:** If the event bus consumer stalls for any reason, `request()` hangs forever.

**Solution:** Add a timeout with fallback to direct rendering:

```python
async def request(self, event: UiEvent, *, timeout: float = 5.0) -> Any:
    if not self.is_running or self._queue is None:
        raise RuntimeError("UI event bus is not running")
    future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    await self._queue.put(_QueuedEvent(event, future))
    try:
        return await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        # Fallback: apply directly to consumer
        if self._consumer is not None:
            result = self._consumer.handle(event)
            if inspect.isawaitable(result):
                result = await result
            return result
        raise
```

This is a safety net — even if the primary fixes work, a timeout prevents indefinite hangs from any future regression.

### Fix 4: Immediate Feedback When `/clear` Is Queued During Agent Execution

**Problem:** User types `/clear` while agent is busy, sees no response.

**Solution:** In `_handle_user_input`, detect `/clear` during busy state and provide immediate feedback:

```python
async def _handle_user_input(self, app, user_input):
    user_input = user_input.strip()
    if user_input.startswith("/"):
        if app._busy:
            # For /clear, show feedback that it's queued
            if user_input == "/clear":
                ui.print("[dim]Clear queued — will execute after current turn.[/dim]")
            ...
```

This doesn't fix the wait, but sets user expectations. A more aggressive option (cancel the agent turn first, then clear) is possible but riskier and can be a separate enhancement.

## Implementation Plan

### Step 1: Add `prefer_direct` and `skip_transcript` to `_show_startup`

**Files:** `src/voidx/agent/graph/run_loop.py`, `src/voidx/agent/graph/contracts.py`

- Add `prefer_direct: bool = False` and `skip_transcript: bool = False` parameters
- Guard the `ui_events.request()` path with `not prefer_direct`
- Guard `_restore_transcript_snapshot()` calls with `not skip_transcript`

### Step 2: Update `_clear()` to use new parameters

**Files:** `src/voidx/agent/slash/session.py`

- Change `await self._show_startup()` to `await self._show_startup(prefer_direct=True, skip_transcript=True)`

### Step 3: Add timeout to `ui_events.request()`

**Files:** `src/voidx/ui/output/events/__init__.py`

- Wrap `await future` in `asyncio.wait_for()`
- Add fallback to direct consumer handle on timeout

### Step 4: Add busy-state feedback for `/clear`

**Files:** `src/voidx/agent/graph/run_loop.py`

- In `_handle_user_input`, check `app._busy` for `/clear` and print a queued message

### Step 5: Tests

**Files:** `tests/` (new or existing)

- Test `_show_startup(prefer_direct=True)` skips `ui_events.request()`
- Test `_show_startup(skip_transcript=True)` skips transcript restoration
- Test `ui_events.request()` timeout falls back to direct handle
- Test `/clear` during busy state shows queued message

## Files Changed

| File | Change |
|------|--------|
| `src/voidx/agent/graph/run_loop.py` | Add params to `_show_startup`, add busy feedback |
| `src/voidx/agent/graph/contracts.py` | Update `_show_startup` signature |
| `src/voidx/agent/slash/session.py` | Use `prefer_direct=True, skip_transcript=True` in `_clear` |
| `src/voidx/ui/output/events/__init__.py` | Add timeout to `request()` |
| `tests/` | New tests for all fixes |

## Out of Scope

- Canceling agent turn on `/clear` (separate enhancement)
- Refactoring `_consume` to support concurrent command processing (architectural change)
- Merging the 3 DB transactions in `clear_current_session` into one (optimization, not a hang fix)
