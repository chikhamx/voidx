# Concurrent Sessions for Web/Desktop UI

> **Status: Done**

## Summary

voidx web/desktop should allow multiple sessions in the sidebar to run at the same time. Each running session must own its own execution task, transcript stream, cancellation handle, pending UI requests, and status. Switching the active session should only change what the user is viewing; it must not stop or redirect background runs.

The first implementation target is web/desktop only. TUI remains single-session because its input loop, frame rendering, and busy state are intentionally single-canvas.

## Concurrency Scope

Concurrency is session-level, where each sidebar session maps to one protocol `thread_id` and one runtime execution context. The system may run multiple different sessions at the same time, up to the configured process/workspace concurrency limit.

Concurrency is not turn-level within a single session. A given `thread_id` can have at most one active turn; a second submit to the same session is rejected with `ERR_TURN_IN_PROGRESS` rather than queued or run in parallel.

The first release should therefore treat the max concurrency setting as "maximum concurrently running sessions in this workspace process", not "maximum agent loops inside one session".

## Problem


The current web/desktop path is effectively single-run:

- `GatewaySession` tracks many threads for listing and transcript snapshots, but command forwarding still goes through one active runtime.
- `PureTui` owns one `_queue`, one `_current_submit_task`, and one busy/cancel state.
- `GraphRunLoopHost` stores mutable current-session state on the host, including `_session`, `_current_tree`, `_session_msg_cache`, runtime snapshots, and permission session state.
- `session.submit` and `session.cancel` operate on the active runtime, not an explicit per-session runner.

This makes simultaneous sessions ambiguous. A second submit may be queued behind the current run, and cancellation can only target the current task. If the UI ever shows multiple sessions as running, that is display state rather than true isolated execution.

## Goals

- Run different sessions concurrently from the web/desktop sidebar.
- Keep all events, transcript updates, approvals, checkpoints, and cancellation scoped to the originating session.
- Let users switch sessions while background sessions continue running.
- Prevent same-session double-submit with a clear `ERR_TURN_IN_PROGRESS` response.
- Preserve existing single-session behavior for TUI.
- Keep the first version safe for workspace writes through a conservative write-lock policy.

## Non-Goals

- TUI multi-session concurrency.
- Multi-workspace process orchestration beyond the current desktop workspace backend.
- Collaborative multi-user execution.
- Parallelizing one session turn across multiple independent agent loops.
- Removing or redesigning existing session persistence.

## Design Overview

Use a session actor/mailbox model as the preferred architecture. A web/desktop `ThreadRunManager` owns one `ThreadActor` per session/thread and delegates `session.submit`, `session.cancel`, and `session.respond` to that actor. Each actor serializes commands for exactly one `thread_id`, so same-session turns cannot overlap, while different session actors may run concurrently up to the configured workspace/process limit.

Each actor owns a `ThreadRunState` containing its task, output tree, adapter, pending requests, model/profile snapshot, cancellation metadata, and mailbox. The manager coordinates cross-session concerns such as concurrency limits, workspace write locks, status snapshots, and reconnect replay.

The agent runtime must stop depending on mutable host-level “current session” state during a concurrent run. Each actor should create an isolated `ThreadExecutionContext` for its session and pass it into a reusable turn runner. Shared services such as settings, MCP manager, LSP manager, and provider registry can remain process-level services, but per-session mutable state must live in the run context.

## Architecture

### New Components

#### `ThreadRunManager`

Suggested path: `src/voidx/ui/gateway/run_manager.py`

Responsibilities:

- Maintain `dict[str, ThreadActor]` and expose each actor's `ThreadRunState`.
- Resolve target `thread_id` for submit, cancel, and respond calls before dispatching to an actor.
- Start a run for a specific `thread_id` by enqueuing a submit command on that thread actor.
- Reject a second run for the same `thread_id` with `ERR_TURN_IN_PROGRESS`.
- Enforce the configured max concurrently running sessions across actors.
- Return `ERR_CONCURRENCY_LIMIT` when a different-thread submit would exceed the limit.
- Cancel a specific running thread by enqueuing a cancel command on that thread actor.
- Route UI responses to the correct actor and pending request.
- Broadcast per-thread status changes and snapshots.
- Own workspace-level write locks for unsafe operations.
- Expose compact runtime status for `workspace.snapshot`.

#### `ThreadActor`

Suggested path: `src/voidx/ui/gateway/run_manager.py`

Responsibilities:

- Own exactly one `thread_id` and one mailbox/queue of session commands.
- Serialize `submit`, `cancel`, `respond`, and status transitions for that session.
- Create one isolated `ThreadExecutionContext` for each turn.
- Keep pending UI requests local to the actor's `ThreadRunState`.
- Never read from or write to the gateway's active thread as a routing fallback once a target `thread_id` has been resolved.
- Notify `ThreadRunManager` before entering or leaving an active state so global concurrency accounting remains accurate.

Recommended command types:

```python
ThreadCommand = SubmitCommand | CancelCommand | RespondCommand
```

The actor mailbox (`asyncio.Queue`) should use `maxsize=2` — enough to accept a submit plus an immediate cancel/respond, but bounded enough to prevent unbounded queuing from rapid frontend requests. A submit arriving at a full mailbox is the same error as an active submit: `ERR_TURN_IN_PROGRESS`.

The actor mailbox is the core same-session concurrency guard: at most one turn task may be active for the actor, and a second submit is rejected rather than queued behind the running turn.

#### `ThreadRunState`

Suggested fields:

```python
class ThreadRunState(BaseModel):
    thread_id: str
    session_id: str
    status: Literal[
        "idle",
        "running",
        "waiting_for_user",
        "waiting_for_write_lock",
        "cancelling",
        "failed",
    ]
    task: asyncio.Task | None
    output_tree: OutputTree
    adapter: UiEventItemAdapter
    pending_requests: dict[str, asyncio.Future[UiResponse]]
    mailbox: asyncio.Queue[ThreadCommand]
    model_provider: str
    model_name: str
    workspace: str
    started_at: float | None
    last_error: str
```

Use a `dataclass` for the real implementation so storing non-Pydantic fields such as `asyncio.Task`, `asyncio.Queue`, and `OutputTree` does not require workarounds.

#### `ThreadExecutionContext`

Suggested path: `src/voidx/agent/graph/thread_context.py`

Responsibilities:

- Hold all mutable per-session state that currently risks being shared through `GraphRunLoopHost`.
- Provide session-local accessors for:
  - `session`
  - `session_msg_cache`
  - `session_date`
  - `current_tree`
  - permission session grants
  - runtime snapshot
  - title generation task
  - cancellation token

This context should be passed through the web/desktop run path. TUI can keep using the existing host-level state until it is intentionally migrated.

### Thread State Machine

Allowed state transitions:

```text
idle -> running
running -> waiting_for_user -> running
running -> waiting_for_write_lock -> running
running -> cancelling -> idle
running -> failed
running -> idle
failed -> running
```

Rules:

- A new submit is allowed only from `idle` or `failed`.
- `waiting_for_user` and `waiting_for_write_lock` still count as active turns for same-thread double-submit protection.
- A cancelled turn must emit `turn.cancelled` before returning to `idle`.
- A failed turn must emit `turn.failed`, append the failure item to that thread transcript, and leave the thread in `failed` until the next successful submit moves it to `running`.
- Background state transitions must update sidebar metadata without switching the active thread.

### Concurrency Limit Policy

The first release should default to max concurrent sessions = 2. This limit applies to concurrently active session actors in this workspace process, including actors in `running`, `waiting_for_user`, `waiting_for_write_lock`, or `cancelling` states.

Rules:

- Same-thread submit while active returns `ERR_TURN_IN_PROGRESS`.
- Different-thread submit below the limit starts immediately.
- Different-thread submit at the limit returns `ERR_CONCURRENCY_LIMIT` with the active `thread_id`s and configured limit.
- The first version should reject over-limit submits rather than queueing them globally; explicit user retry is simpler and avoids hidden background work.
- Cancelled, failed, or completed turns release one concurrency slot when their terminal event has been emitted and state accounting is updated.

## Protocol Changes

### Thread Status

Use one canonical thread status enum across `ThreadRunState`, protocol v2, gateway snapshots, and frontend state:

```python
ThreadStatus = Literal[
    "idle",
    "running",
    "waiting_for_user",
    "waiting_for_write_lock",
    "cancelling",
    "failed",
]
```

Rules:

- `idle`: no active turn and no pending prompt.
- `running`: the turn task is executing or streaming output.
- `waiting_for_user`: the turn is blocked on permission, clarify, checkpoint, or another UI request.
- `waiting_for_write_lock`: the turn is ready to execute a write-risk operation but another session holds the workspace write lock.
- `cancelling`: cancellation has been requested and the task is unwinding.
- `failed`: the latest turn failed and the failure item has been appended to that thread transcript.

The existing `ThreadInfo.status` protocol field must expand from `Literal["idle", "running"]` to this enum before the frontend relies on background state.


### Turn Status

The `TurnInfo.status` field must expand from `Literal["running", "completed", "cancelled"]` to include `"failed"`:

```python
TurnStatus = Literal["running", "completed", "cancelled", "failed"]
```

Rules:

- `running`: the turn is actively executing (including streaming output or waiting for a user response).
- `completed`: the turn finished without error.
- `cancelled`: the turn was cancelled by the user before completion.
- `failed`: the turn crashed with an unhandled exception; the error item has been appended to the thread transcript.

The frontend should render a `failed` turn with an error indicator distinct from `cancelled`. The thread remains `failed` until the next successful submit.

### `session.submit`

Current:

```json
{ "text": "hello" }
```

New:

```json
{ "thread_id": "session-id", "text": "hello" }
```

Rules:

- `thread_id` is optional for compatibility and defaults to the active thread.
- If the target thread is already running, return `ERR_TURN_IN_PROGRESS`.
- Successful submit immediately marks that thread `running` and emits a snapshot/status update.

### `session.cancel`

Current:

```json
{}
```

New:

```json
{ "thread_id": "session-id" }
```

Rules:

- `thread_id` is optional and defaults to active thread.
- Cancelling a non-running thread returns `{ "ok": true, "status": "idle" }`.
- Cancelling a running thread marks it `cancelling`, cancels its task, and eventually emits `turn.cancelled`.

### `session.respond`

Current:

```json
{ "request_id": "req", "value": "allow" }
```

New:

```json
{ "thread_id": "session-id", "request_id": "req", "value": "allow" }
```

Rules:

- `thread_id` is optional only when `request_id` is globally unique.
- The manager should prefer `(thread_id, request_id)` lookup.
- If omitted, lookup by request id is allowed only when exactly one pending request matches.
- Ambiguous request ids return a parameter error instead of guessing.

Implementation requirements:

- Move pending request ownership from the single gateway-level `_pending_requests` map into manager-owned per-thread state, or wrap it with an index keyed by `(thread_id, request_id)`.
- Include `thread_id` in every `ui.request` notification. If the underlying `UiRequest` model does not carry it, the manager/gateway adapter must add it before broadcasting.
- Keep request ids globally unique as a best effort, but do not depend on that for correctness.
- When a response arrives for a failed, cancelled, or unknown thread, return a params error instead of resolving a request on the active thread.

### Events

All runtime notifications that represent a running turn must include `thread_id`:

- `turn.started`
- `turn.completed`
- `turn.failed`
- `turn.cancelled`
- `item.started`
- `item.delta`
- `item.completed`
- `ui.request`
- prompt item notifications

The frontend must use this `thread_id` for routing and must not infer ownership from the current active session.

Implementation requirements:

- Add typed UI event schemas for turn terminal states, for example `TurnCompleted`, `TurnFailed`, and `TurnCancelled` alongside the existing `TurnStarted` event.
- Add adapter mappings that emit `turn.completed`, `turn.failed`, and `turn.cancelled` with `thread_id`, `turn_id`, elapsed timing where available, and failure details for failed turns.
- Expand `TurnInfo.status` from `Literal["running", "completed", "cancelled"]` to include `"failed"`.
- Emit exactly one terminal turn event for each started turn, including cancellation and exception paths.
- Update manager-owned `ThreadRunState.status` and protocol `ThreadInfo.status` before broadcasting the terminal event snapshot.

## Runtime Isolation

The implementation should separate shared services from per-session state.

Shared process-level services:

- settings loader
- provider/profile registry
- MCP manager
- LSP manager
- gateway server
- desktop process

Per-session state:

- session metadata and model snapshot
- message cache
- runtime snapshot
- output tree
- transcript stream
- pending UI requests
- title generation
- permission session grants
- cancellation state
- active tool execution list

Before enabling different-thread concurrency, migrate or wrap every graph API that reads or mutates host-level current-session fields. The minimum migration boundary is:

- `_session` and `_session_date`
- `_current_tree` and `_turn_node`
- `_session_msg_cache` and context-compaction caches that mutate it
- `_task_state`, runtime snapshots, and runtime guard state
- permission session grants and pending permission prompts
- title generation counters/tasks
- cancellation token and active tool execution state
- transcript persistence calls that currently infer the active session

The first implementation can use a wrapper around existing graph methods only if tests prove the wrapper does not mutate host-level current-session fields while another session is running. If that cannot be guaranteed, add context-aware graph methods before enabling parallel submits.

## Gateway Behavior

`GatewaySession` remains responsible for JSON-RPC transport, client broadcast, thread listing, and snapshots. It should delegate active execution to `ThreadRunManager`.

Expected changes:

- Store one adapter per thread as it does today, but update adapters from the manager-owned output events.
- Build `WorkspaceSnapshot.threads` with manager statuses.
- Build `active_snapshot` from the active thread's manager-owned `OutputTree` when running, otherwise from persisted transcript.
- Keep `session.switch` view-only. Switching to a running thread should be allowed in web/desktop because running threads can now be viewed.
- Remove the current restriction that rejects switching to a running thread for web/desktop. TUI can keep old behavior if it still uses the legacy path.
- Note: the current `switch_thread` implementation uses `ERR_TURN_IN_PROGRESS` (code -32001) for "thread is running, cannot switch". This code should be either removed entirely (web/desktop allows switching to running threads) or replaced with a distinct error code reserved for same-thread double-submit when switching is genuinely unsupported (TUI path). `ERR_TURN_IN_PROGRESS` must retain its spec meaning: "duplicate submit to the same thread".

## Frontend Behavior

### Sidebar

- Each session row can display `idle`, `running`, `waiting_for_user`, `waiting_for_write_lock`, `cancelling`, or `failed`.
- Running rows show a small stop action on hover.
- Sessions waiting on approval/clarify/checkpoint show a badge or subtle warning indicator.
- Clicking a session switches the active view without affecting the run.

### Composer

- The composer submits to the active `thread_id`.
- If the active session is running, the send button becomes a stop button for that thread.
- If another session is running in the background, the active idle session can still submit.
- If the active session is waiting for user input, the composer should remain disabled until the request is answered or cancelled.

### Transcript

- Active transcript renders only the active thread.
- Background item events (item.started, item.delta, item.completed) update the thread's cached snapshot but must not modify the currently rendered transcript unless that thread becomes active.
- All incoming events from the gateway must be routed by `thread_id`. The frontend should never infer ownership from the current active thread — a non-active thread's events must be silently consumed into the cache.
- Switching to a running thread renders the latest cached state and then continues streaming live events for that thread.

### Requests

Permission, clarify, and checkpoint prompts must display the originating session title/workspace. The first version should support both:

- modal prompt when the active session needs input
- sidebar badge when a background session needs input

If a background session prompt arrives, prefer not to steal focus. Show a badge and let the user switch, unless the request is high-risk and requires immediate visibility.

## Permission, Checkpoint, and Write Safety

Concurrency introduces workspace mutation hazards. The first version should use a conservative write lock.

### Workspace Write Lock

- Read-only tools and LLM calls may run concurrently.
- File writes, shell commands with write risk, checkpoint creation, rollback, and apply-patch operations require a workspace-level write lock.
- Lock acquisition timing: acquire the lock just before executing the write-risk tool, not at turn start. This allows multiple sessions to run read-only LLM turns concurrently without blocking on the lock.
- Use FIFO lock acquisition for the first release so waiting sessions are fair and predictable.
- If a session requests the write lock while another session holds it, mark the session `waiting_for_write_lock` and show the lock holder plus queued session count in the UI.
- Waiting sessions must remain cancellable before the write-risk tool starts.
- Releasing the lock must happen in `finally`/terminal-turn cleanup so cancellation and failure cannot leave the workspace permanently locked.
- Rollback and checkpoint restore operations must acquire the same write lock and should be rejected while another session is actively mutating the workspace.

### Permissions

- Permission prompts must include `thread_id`.
- Session-scoped approvals apply only to that session.
- Global approvals remain global only when the existing permission policy explicitly allows them.

### Checkpoints

- Checkpoint ids must include or be mapped to `thread_id`.
- Rollback should be blocked while any other session holds the write lock.
- If two sessions have pending checkpoints, resolving one must not clear the other's prompt.

## Persistence

Concurrent runs should keep transcript persistence session-local:

- Append messages to the target session id.
- Persist runtime snapshots per session.
- Persist model provider/model used by that session turn.
- Avoid using the active session id when writing transcript rows.

If a background session finishes while inactive, the sidebar metadata should update title, message count, timestamp, and status without forcing a view switch.

## Error Handling

- Same-thread submit while active: `ERR_TURN_IN_PROGRESS`.
- Different-thread submit above the configured limit: `ERR_CONCURRENCY_LIMIT` with `{ "limit": n, "active_thread_ids": [...] }`.
- Unknown thread: method params error.
- Cancel missing thread: method params error.
- Cancel idle thread: ok no-op.
- Ambiguous `session.respond` without `thread_id`: method params error.
- Response for failed, cancelled, or unknown thread: method params error.
- Background run failure: mark the thread failed, append an error item to that thread transcript, release any held concurrency/write-lock slots, and keep other runs alive.
- Actor task crash (uncaught exception in the mailbox processing loop): the actor's `asyncio.Task` is wrapped by `ThreadRunManager`. If it exits abnormally, the manager must catch it, mark the thread `failed`, release all held resources, and log the crash. The thread stays `failed` until the next submit restarts the actor.
- Actor task cleanup in `finally`: every `ThreadActor.run()` cycle must release concurrency slots, write locks, cancellation tokens, and pending-request futures in a `finally` block so that crashes and cancellations cannot leave dangling state.
- Gateway disconnect: keep running tasks alive; reconnect should replay snapshots for all threads.

## Migration Strategy

Phase 1 should preserve existing single-session behavior and add explicit thread ids:

1. Extend protocol and frontend calls to always send `thread_id`.
2. Keep old default-to-active behavior for compatibility.
3. Route every submit-capable gateway method through an explicit target thread, including `session.submit`, `commands.run`, `/guide` or guidance submission paths, and any command catalog item that internally creates a `UiSubmitCommand`.
4. Add tests proving routing does not depend on active session.

Phase 2 introduces `ThreadRunManager` and `ThreadActor` without enabling multi-session execution yet:

1. Add `ThreadRunManager`, `ThreadActor`, `ThreadRunState`, and command mailbox types.
2. Route submit/cancel/respond through the actor for the target `thread_id`.
3. Preserve a global single-running-session limit initially.
4. Keep same-thread submit rejection inside the actor, not in frontend-only state.
5. Prove state ownership, prompt routing, cancellation routing, and reconnect snapshots with tests.

Phase 3 enables read-only different-thread concurrency:

1. Add per-session execution contexts and remove active-session routing from the web/desktop run path.
2. Set max concurrent sessions to the first-release default of 2.
3. Allow different thread actors to run concurrently when below the limit.
4. Return `ERR_CONCURRENCY_LIMIT` instead of globally queueing over-limit submits.
5. Verify two long read-only turns can run concurrently and complete independently.

Phase 4 enables write-risk operations under the workspace write lock:

1. Add FIFO workspace write-lock acquisition around file writes, write-risk shell commands, checkpoints, rollback, and apply-patch operations.
2. Mark sessions waiting on the lock as `waiting_for_write_lock` and keep them cancellable before tool start.
3. Release the lock on success, failure, and cancellation through terminal-turn cleanup.
4. Update UI for lock holder, waiting badge, and queued count.

Phase 5 hardens persistence and reconnect:

1. Reconnect with running, waiting, cancelling, and failed thread snapshots.
2. Persist background completions and failures to the owning session.
3. Add failure recovery tests for actor crash, gateway disconnect, cancellation, and lock release.

## Testing Plan

### Backend Unit Tests

- `session.submit` with explicit `thread_id` starts the correct thread.
- Same thread double-submit returns `ERR_TURN_IN_PROGRESS`.
- Different thread submits can both enter running state.
- `session.cancel(thread_id=A)` cancels A and leaves B running.
- `session.respond(thread_id=A, request_id=X)` resolves only A's pending request.
- Ambiguous response without `thread_id` fails.
- Background failure marks only that thread failed.
- Over-limit different-thread submit returns `ERR_CONCURRENCY_LIMIT` and does not enqueue hidden work.
- `ThreadActor` rejects same-thread double-submit even if the frontend sends it.
- Concurrency slot accounting is released after completed, failed, and cancelled turns.

### Gateway Tests

- Events include `thread_id`.
- Active snapshot switches between cached thread trees.
- Switching to a running thread is allowed in web/desktop mode.
- Reconnect returns all running statuses.
- Reconnect returns waiting, cancelling, and failed statuses.
- Workspace snapshot includes active thread ids, max concurrency, and write-lock holder metadata.

### Frontend Tests

Convention: any stateful frontend module (e.g. `sidebar.ts` extensions for concurrent thread state, or a new `run-manager.ts`) must export a `_resetForTest()` function that clears module-level state, called in a `beforeEach` block.
- Submitting in active session sends active `thread_id`.
- Active running session shows stop button.
- Background running session keeps active idle composer enabled.
- Stop action on sidebar row cancels that row's `thread_id`.
- Background prompt shows a badge and does not overwrite the active transcript.
- Switching to a running session renders its cached transcript and live stream.

### Integration Tests

- Two sessions run long read-only turns concurrently.
- Cancelling one long turn does not stop the other.
- Two write attempts serialize on the workspace write lock.
- A waiting write-lock session can be cancelled before the write-risk tool starts.
- Write lock is released after write-risk tool failure or cancellation.
- A third concurrent session submit returns `ERR_CONCURRENCY_LIMIT` when the first-release limit is 2.
- Permission prompt in one session does not block another read-only session.

## Open Questions

- Should users be able to configure a max concurrent session count, or should this be fixed at a small default such as 2 or 3?
- Should background prompts ever open a global modal, or should they always be sidebar badges until selected?
- Should write-lock waiting sessions be cancellable before the tool starts? Recommended answer: yes.
- ~~Should `/guide` target only the active running session, or require selecting a target session when multiple sessions are running?~~ **Resolved**: target only the currently active session. `/guide` is an inline user action — its expected scope is whatever the user is currently looking at. Multi-session `/guide` selection can be added as a future enhancement if users ask for it.
- Should title generation be allowed to run concurrently with active turns, or should it use a separate low-priority queue?

## Recommended Defaults

- Max concurrent sessions: 2 for the first release.
- Same-session double-submit: reject, do not queue.
- Different-session submit: allow if below max concurrency.
- Write tools: workspace-level lock.
- Background prompts: badge first, no focus steal.
- TUI: unchanged single-session behavior.

