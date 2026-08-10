# Subagent Todo Isolation

Date: 2026-08-07

> **Status: Approved design; awaiting implementation**

## Goal

Stop child-agent `todo` updates from hijacking the parent session's pinned Todo panel and runtime todo state.

After this change:

- main-agent pinned Todo only reflects main-agent todo tool calls;
- subagent todo updates render under that subagent's transcript node, not the global pin;
- subagent todo state does not overwrite parent `TaskState.todo_state` or parent `TaskTracker._todos`;
- existing main-agent todo pin / commit / clear behavior remains unchanged.

## Current State

Relevant code:

- `src/voidx/agent/adapters/langgraph/runtime/subagent.py`
  - child todo tool results currently call `ui_port.events.emit_direct(TodoUpdated(...))`
  - also call `todo_state_sink(todo_state)` when present
- `src/voidx/agent/adapters/langgraph/execution.py`
  - queues `SubagentStarted(...)` before entering `run_subagent()`
  - passes `todo_state_sink=lambda todo_state: apply_todo_state_to_host(self, todo_state)`
- `src/voidx/agent/application/todo_state.py`
  - `apply_todo_state_to_host()` writes into host `_task_state.todo_state` and host tracker todos
- `src/voidx/agent/adapters/tools/plugins.py`
  - parent registry installs plugin-wrapped `TodoWriteTool(tracker=parent_tracker)`
- `src/voidx/tooling/application/registry.py`
  - `filtered_copy()` shallow-copies tool instances, so child registry still holds the parent todo plugin
- `src/voidx/presentation/output/events/consumers.py`
  - `TodoUpdated` always calls `dock.set_todo_state(...)` and ignores `agent_id`
  - fallback subagent nodes may be replaced when a queued `SubagentStarted` is consumed
- `src/voidx/presentation/output/dock/app.py`
  - single global `_todo_state` pin used by TUI
- `tui/voidx_cli/render_todo.py`
  - renders only `dock.todo_state()`
- `src/voidx/agent/adapters/langgraph/runtime/turn_runner.py`
  - turn end emits parent-scoped `TodoCommitted()` / `TodoCleared()`

Observed failure:

```text
Juno(subagent) updates todos
  -> TodoUpdated(agent_id=0, summary="4/4 done ...")
  -> DockEventConsumer.set_todo_state(...)
  -> global pin shows child todos as if they were main-agent work
```

Root cause chain:

```text
child TodoWriteTool shares parent tracker
  -> child todo write mutates parent tracker todos
  -> subagent emits TodoUpdated(agent_id>=0)
  -> consumer ignores agent_id and pins globally
  -> todo_state_sink also copies child TodoRunState into parent TaskState
```

Independent ordering hazard:

```text
execution queues SubagentStarted(agent_id=0)
  -> run_subagent emits TodoUpdated(agent_id=0) via emit_direct
  -> direct event bypasses the queued SubagentStarted
  -> _agent_parent(0) creates a fallback subagent node and attaches the todo
  -> queued SubagentStarted later replaces/removes the fallback subtree
  -> child todo snapshot disappears from the transcript
```

Historical design conflict:

- `docs/archive/2026-06/2026-06-07/todo-ui-design-2026-06-06.md` wanted `agent_id >= 0` todos under the subagent node.
- `docs/archive/2026-06/2026-06-10/todo-run-state-design-2026-06-10.md` later chose one session-level todo state.

This spec supersedes the shared-session choice for subagent todos. Isolation is now the intended behavior.

## Non-goals

This change does not:

- redesign the todo tool schema or ops (`write` / `update` / `read`);
- add multi-pin UI for concurrent main-agent todos;
- change frontend dock layout beyond ignoring child-agent todo pin updates;
- make subagent todos persist into parent runtime state for later parent LLM turns;
- change parent turn-end `TodoCommitted` / `TodoCleared` semantics for main-agent todos;
- redesign subagent report protocol or workflow tooling.

## Design

### 1. UI: isolate child `TodoUpdated` from the global pin

File: `src/voidx/presentation/output/events/consumers.py`

Rules:

1. `TodoUpdated` with `agent_id < 0` keeps current behavior:
   - `dock.set_todo_state(summary, items)`
   - remains pinned until parent `TodoCommitted` / `TodoCleared`
2. `TodoUpdated` with `agent_id >= 0` must **not** call `set_todo_state`.
3. Child non-empty todo updates create or update a single `node_type="todo"` child under `_agent_parent(agent_id)`.
4. Child empty todo writes clear that subagent todo node:
   - if a `node_type="todo"` child exists under the subagent parent, remove it (or clear header/body/payload and drop it from the tree)
   - do not leave a stale child todo snapshot after an empty write
   - do not touch the global pin
5. Child todo nodes are settled snapshots:
   - `status="done"`
   - header/body from existing dock todo render helpers
   - payload stores `{summary, items}`
6. Child `todo` `read` remains side-effect free:
   - no `TodoUpdated` event (existing `todo_run_state_from_result` short-circuit)
   - no pin change
   - no subagent todo node create/update/clear
7. Parent-scoped `TodoCommitted` / `TodoCleared` continue to operate only on the global pin.
   - They must not clear or rewrite child todo nodes already attached under subagent trees.
   - No new child-scoped commit event is required for this fix.

Suggested consumer shape:

```python
case TodoUpdated() as e:
    if e.agent_id >= 0:
        if not e.items:
            return self._clear_subagent_todo_node(e.agent_id)
        return self._upsert_subagent_todo_node(e)
    return self._dock.set_todo_state(e.summary, e.items)
```

`_upsert_subagent_todo_node(event)`:

- resolve `parent = self._agent_parent(event.agent_id)`
- find existing child with `node_type == "todo"` under that parent
- if missing, `dock.tree.new_node(parent=parent, node_type="todo", ...)`
- update `header`, `body_lines`, `payload`, mark settled, refresh

`_clear_subagent_todo_node(agent_id)`:

- resolve `parent = self._agent_parent(agent_id)`
- remove the existing `node_type == "todo"` child if present
- refresh
- leave parent pin untouched

`SubagentStarted` fallback reconciliation:

1. Read `fallback = self._agent_nodes.get(event.agent_id)` before selecting the canonical node.
2. If the matching parent `agent` tool node exists, that tool node becomes canonical:
   - move every child from `fallback` to the canonical node while preserving order;
   - recompute depths and sibling flags;
   - remove the now-empty fallback;
   - populate the canonical node with `SubagentStarted` metadata.
3. If no matching tool node exists and `fallback` exists, promote the fallback in place by applying the `SubagentStarted` metadata instead of creating a second subagent node.
4. If neither canonical tool node nor fallback exists, create the subagent node as today.
5. After reconciliation, `_agent_nodes[event.agent_id]` must point to the only canonical subagent node.

Use the existing helpers in `src/voidx/presentation/output/dock/todo.py`:

- `todo_state_from_items`
- `render_todo_header`
- `render_todo_state_lines`
- `todo_state_payload`

Optional dock helper is allowed if it keeps consumer thin, for example:

```python
def upsert_todo_node(self, parent: OutputNode, summary: str, items: Sequence[Any]) -> OutputNode: ...
```

But the global pin API stays main-agent only:

- `set_todo_state`
- `commit_todo_state`
- `clear_todo_state`
- `todo_state`

### 2. Event ordering: queue child todo updates behind subagent startup

Files:

- `src/voidx/agent/adapters/langgraph/execution.py`
- `src/voidx/agent/adapters/langgraph/runtime/subagent.py`

`execution.py` already sends `SubagentStarted` through `await self._ui.events.emit(...)`. Child todo updates must use that same queue:

```python
if ui_port.via_events() and tid == "todo":
    todo_event = todo_updated_event(result, agent_id=agent_id)
    if todo_event is not None:
        await ui_port.events.emit(todo_event)
```

Hard rules:

1. Do not use `emit_direct()` for child `TodoUpdated`.
2. A child todo update must be enqueued after `SubagentStarted` and before the corresponding `SubagentFinished` for that child run.
3. Do not rely on `_agent_parent()` fallback creation as the normal ordering mechanism.
4. Keep the existing fallback behavior defensive for malformed or externally replayed event sequences.
5. If a fallback node nevertheless exists when `SubagentStarted` arrives, migrate its children to the canonical subagent node before removing the fallback; this prevents malformed, replayed, or future event sources from silently losing a todo snapshot.
6. Gateway mirroring may remain asynchronous; ordering is established by the primary `UiEventBus` queue before mirror scheduling.

This is intentionally scoped to child todo events. Other direct child status/tool events are unchanged unless a focused test proves they require the same migration.

### 3. Runtime: stop copying child todo state into the parent session

File: `src/voidx/agent/adapters/langgraph/execution.py`

Remove the parent sink wiring:

```python
"todo_state_sink": lambda todo_state: apply_todo_state_to_host(self, todo_state),
```

Child runs already maintain `sub_task_state.todo_state` inside `run_subagent()`. That remains the child-local runtime source of truth for the child turn.

File: `src/voidx/agent/adapters/langgraph/runtime/subagent.py`

Keep:

- local `sub_task_state.todo_state` updates from tool results
- `TodoUpdated(agent_id=agent_id, ...)` emission for UI

Change:

- parent orchestrator must stop passing `todo_state_sink`
- if the `todo_state_sink` parameter remains for tests/back-compat, it is **child-local only**
- any remaining sink must not call `apply_todo_state_to_host` on the parent host
- preferred production path: omit the sink entirely so child todo state stays in `sub_task_state` only

Do **not** call `apply_todo_state_to_host(parent, child_todo_state)` anymore.

### 4. Runtime: give the child its own todo tracker

Root leak #2 is tool-instance sharing:

- parent builds `TodoWriteTool(tracker=parent_tracker)`
- child does `parent_tools.filtered_copy(...)`
- child todo writes mutate `parent_tracker._todos`

Required fix in `run_subagent()` after the child registry is created:

1. Inspect `agent_tools.get("todo")` after the child registry is created and blocked tools are filtered.
2. If `todo` is present, create `child_todo_tracker = TaskTracker()` and replace it with a plugin-wrapped `TodoWriteTool` backed by that tracker.
3. If `todo` is absent in an intentionally reduced test registry, leave it absent.
4. Keep using the parent `tracker` argument only for worker-task status (`start` / `update` / `finish`), not for todo item storage.

Plugin-aware replacement is mandatory because the production parent registry stores `AgentToolPlugin` wrappers rather than bare `TodoWriteTool` instances. Preserve the copied todo plugin's runtime binding when constructing the child replacement; the later existing `bind_agent_tool_runtime(agent_tools, agent_runtime)` call will then bind all copied child plugins to the final child runtime.

```python
from voidx.agent.adapters.tools.plugins import AgentToolPlugin
from voidx.agent.adapters.tools.todo import TodoWriteTool

copied_todo_plugin = agent_tools.get("todo")
if copied_todo_plugin is not None:
    if not isinstance(copied_todo_plugin, AgentToolPlugin):
        raise RuntimeError("child todo tool must use AgentToolPlugin")

    child_todo_tracker = TaskTracker()
    wrapped_todo_plugin = AgentToolPlugin(
        TodoWriteTool(tracker=child_todo_tracker),
        copied_todo_plugin.runtime,
    )
    agent_tools.replace(
        "todo",
        wrapped_todo_plugin,
        wrapped_todo_plugin.description,
        wrapped_todo_plugin.parameters_schema(),
    )
```

Do not install a bare `TodoWriteTool` into a registry that expects `ToolPlugin` wrappers. If a test-only registry intentionally omits `todo`, leave it absent; do not widen the child's tool surface.

Notes:

- Do not clear or rewrite parent tracker todos when a child starts/finishes.
- Child `todo` read/update/write must only see child-local items.
- Parent later `todo` read/update/write must continue from the pre-child parent list.
- Preserve tool schema/description and runtime binding so child execution context still works.

### 5. Gateway / frontend behavior

File: `src/voidx/presentation/gateway/adapter.py`

Hard rule for this phase:

- `TodoUpdated` with `agent_id >= 0` must **not** emit a global todo pin notification
- current frontend `kind === "todo"` always calls `renderTodoInDock(...)`, so any child todo notification would reintroduce the screenshot bug on web/desktop

Required adapter behavior:

```python
def _on_todo_updated(self, event: TodoUpdated) -> JsonRpcNotification | None:
    if event.agent_id >= 0:
        return None  # or otherwise skip global pin item emission
    return self._item_notification(..., "todo", "started", {...})
```

Notes:

- TUI isolation via `DockEventConsumer` remains the primary transcript path.
- Child todo visibility in web/desktop transcript can remain best-effort for this phase.
- Do not expand frontend scope unless needed for parity tests already present.
- Add a gateway test proving child `TodoUpdated` does not produce a global todo pin item.

### 6. Invariants

After implementation, these must hold:

1. Main-agent todo pin content equals the latest main-agent todo tool snapshot only.
2. A child todo write never changes `dock.todo_state()` while a parent pin is active or empty.
3. A child todo write never changes parent `TaskState.todo_state`.
4. A child todo write never changes parent `TaskTracker` todo items.
5. Child non-empty todo UI is nested under the corresponding `subagent` node.
6. Child empty todo write removes that subagent todo node and leaves parent pin untouched.
7. Child `todo` read has no UI or runtime side effects beyond returning current child-local state.
8. Parent turn-end `TodoCommitted` still commits only the parent pin to a root transcript todo node.
9. Parent `TodoCleared` still clears only the parent pin.
10. Two concurrent children keep independent todo trackers and independent subagent todo nodes.
11. Normal production delivery observes `SubagentStarted` before that child's first `TodoUpdated`.
12. Replacing a defensive fallback subagent node never silently loses an attached todo subtree.

## File Changes

| File | Responsibility |
|------|----------------|
| `src/voidx/presentation/output/events/consumers.py` | Branch on `agent_id`; upsert/clear subagent todo nodes; migrate fallback children when canonical startup arrives; keep global pin main-only |
| `src/voidx/presentation/output/dock/app.py` | Optional helper to upsert/clear a non-pinned todo node under a parent |
| `src/voidx/agent/adapters/langgraph/execution.py` | Stop passing parent `todo_state_sink` |
| `src/voidx/agent/adapters/langgraph/runtime/subagent.py` | Install plugin-wrapped child-local todo tracker; keep local todo state; queue child todo UI events |
| `src/voidx/presentation/gateway/adapter.py` | Hard-skip global todo pin notifications for `agent_id >= 0` |
| `src/tests/test_presentation/gateway/test_ui_events_todo.py` | Rewrite `agent_id` isolation expectations; cover empty writes, multiple children, queued startup ordering, and fallback migration |
| `src/tests/test_presentation/gateway/test_adapter.py` | Prove child todo events do not emit global pin items |
| `src/tests/test_agent/adapters/langgraph/runtime/test_subagent_step_budget.py` | Assert local state/tracker isolation and queued child todo event delivery |
| `src/tests/test_agent/adapters/langgraph/runtime/test_todo_events.py` | Cover child todo event construction with `agent_id` and side-effect-free reads |

## Tests

### UI / consumer

Rewrite (blocking):

- `test_todo_updated_with_agent_id_updates_global_root_todo`
  - rename to isolation semantics, e.g. `test_todo_updated_with_agent_id_stays_under_subagent`

New expected behavior:

```text
SubagentStarted(agent_id=0)
TodoUpdated(agent_id=0, items=[...])

assert dock.todo_state() is None
assert no root todo node yet
assert subagent children contain one todo node with the child payload

TodoCommitted()  # parent-scoped turn commit

assert dock.todo_state() is still None
assert still no root todo node created from the child update
assert subagent todo node remains under the subagent
```

Add/adjust:

- parent pin remains intact when a child todo arrives:
  1. main `TodoUpdated(agent_id=-1, ...)`
  2. child `TodoUpdated(agent_id=0, ...)`
  3. `dock.todo_state()` still equals the main snapshot
  4. subagent tree has its own todo node
- child empty write clears only the subagent todo node:
  1. child non-empty `TodoUpdated(agent_id=0, items=[...])`
  2. child empty `TodoUpdated(agent_id=0, items=[])`
  3. subagent has no todo child
  4. parent pin unchanged
- two concurrent children:
  1. `TodoUpdated(agent_id=0, ...)` and `TodoUpdated(agent_id=1, ...)`
  2. each subagent node has its own todo child
  3. neither overwrites the other
  4. parent pin remains untouched
- production event ordering (blocking regression):
  1. call `await bus.emit(SubagentStarted(agent_id=0, ...))` without draining the queue
  2. enqueue the first child `TodoUpdated(agent_id=0, ...)` through the same `await bus.emit(...)` path used by `run_subagent()`
  3. drain the bus
  4. assert there is exactly one `subagent` node for `agent_id=0`
  5. assert its `agent_run_id` and metadata come from `SubagentStarted`
  6. assert the todo node remains attached beneath it
- defensive fallback replacement:
  1. deliver a child `TodoUpdated` before `SubagentStarted` to force `_agent_parent()` fallback creation
  2. deliver `SubagentStarted(agent_id=0, ...)`
  3. assert there is exactly one canonical subagent node
  4. assert its metadata comes from `SubagentStarted` and it retains the todo subtree

### Runtime isolation

Add focused coverage around `run_subagent()`:

1. Parent tracker starts with todos `A`.
2. Child executes todo write with todos `B`.
3. Parent tracker still has `A`.
4. Parent task state todo remains `A` / previous parent value.
5. Child UI event is still produced with `agent_id >= 0` when events are enabled.
6. The event is sent through awaited `events.emit(...)`, not `emit_direct(...)`.
7. With `SubagentStarted` already queued, the todo event is observed after startup and before `SubagentFinished`.
8. Child empty write clears only child-local tracker/state, not parent.
9. Two concurrent children writing different todo lists do not cross-contaminate parent or each other.

If existing sink tests currently assert parent mutation, invert them to assert non-mutation.

### Gateway

Add:

- child `TodoUpdated(agent_id>=0)` does not emit a global todo pin notification
- parent `TodoUpdated(agent_id<0)` still emits the existing todo pin notification

### Commands

```bash
./test.py --backend -- src/tests/test_presentation/gateway/test_ui_events_todo.py -q
./test.py --backend -- src/tests/test_presentation/gateway/test_adapter.py -k todo -q
./test.py --backend -- src/tests/test_agent/adapters/langgraph/runtime/test_todo_events.py -q
./test.py --backend -- src/tests/test_agent/adapters/langgraph/runtime/test_subagent_step_budget.py -q
./test.py --backend -- tui/tests/test_terminal_input.py -k todo -q
```

## Risks

- Existing test `test_todo_updated_with_agent_id_updates_global_root_todo` encodes the old shared-pin semantics and is a blocking rewrite.
- Shallow tool-registry copy means tracker isolation is mandatory; UI-only filtering is insufficient.
- Child todo tool replacement must preserve `AgentToolPlugin` wrapping/runtime binding or child todo execution can break.
- `SubagentStarted` is queued while child todo currently uses `emit_direct`; leaving that mismatch can create and then delete a fallback todo subtree.
- Changing only the consumer is insufficient: ordering must be fixed at the event producer, and fallback migration remains a defensive requirement.
- Child todo nodes under subagent trees may increase vertical transcript noise for chatty reviewers; acceptable because the alternative pollutes the main pin.
- If gateway still emits child todo pin notifications, web/desktop will keep the screenshot bug even after TUI is fixed.

## Rollback

Revert the consumer branch and fallback migration, restore child todo `emit_direct`, restore parent `todo_state_sink`, and restore shared `TodoWriteTool` tracker usage. No schema migration is involved.

## Acceptance Criteria

1. Reproducing the screenshot scenario no longer shows child todos in the bottom `Todo:` pin.
2. Child todos appear under the canonical child agent node in TUI transcript output.
3. Parent todo pin and parent runtime todo state survive concurrent child todo updates.
4. Child empty todo write clears only the child todo node / child-local state.
5. Gateway does not emit global todo pin notifications for child agents.
6. A queued `SubagentStarted` followed by the first child todo update produces exactly one canonical subagent node containing the todo.
7. A forced todo-before-startup fallback sequence migrates the todo subtree instead of dropping it.
8. Focused backend tests above are green.
