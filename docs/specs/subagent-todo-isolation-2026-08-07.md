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

- `src/voidx/agent/infrastructure/langgraph/runtime/subagent.py`
  - child todo tool results emit `TodoUpdated(agent_id=agent_id, ...)`
  - also call `todo_state_sink(todo_state)` when present
- `src/voidx/agent/infrastructure/langgraph/execution.py`
  - passes `todo_state_sink=lambda todo_state: apply_todo_state_to_host(self, todo_state)`
- `src/voidx/agent/application/todo_state.py`
  - `apply_todo_state_to_host()` writes into host `_task_state.todo_state` and host tracker todos
- `src/voidx/agent/adapters/tools/plugins.py`
  - parent registry installs `TodoWriteTool(tracker=parent_tracker)`
- `src/voidx/tooling/application/registry.py`
  - `filtered_copy()` shallow-copies tool instances, so child registry still holds the parent `TodoWriteTool`
- `src/voidx/presentation/output/events/consumers.py`
  - `TodoUpdated` always calls `dock.set_todo_state(...)` and ignores `agent_id`
- `src/voidx/presentation/output/dock/app.py`
  - single global `_todo_state` pin used by TUI
- `tui/voidx_cli/render_todo.py`
  - renders only `dock.todo_state()`
- `src/voidx/agent/infrastructure/langgraph/runtime/turn_runner.py`
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

### 2. Runtime: stop copying child todo state into the parent session

File: `src/voidx/agent/infrastructure/langgraph/execution.py`

Remove the parent sink wiring:

```python
"todo_state_sink": lambda todo_state: apply_todo_state_to_host(self, todo_state),
```

Child runs already maintain `sub_task_state.todo_state` inside `run_subagent()`. That remains the child-local runtime source of truth for the child turn.

File: `src/voidx/agent/infrastructure/langgraph/runtime/subagent.py`

Keep:

- local `sub_task_state.todo_state` updates from tool results
- `TodoUpdated(agent_id=agent_id, ...)` emission for UI

Change:

- parent orchestrator must stop passing `todo_state_sink`
- if the `todo_state_sink` parameter remains for tests/back-compat, it is **child-local only**
- any remaining sink must not call `apply_todo_state_to_host` on the parent host
- preferred production path: omit the sink entirely so child todo state stays in `sub_task_state` only

Do **not** call `apply_todo_state_to_host(parent, child_todo_state)` anymore.

### 3. Runtime: give the child its own todo tracker

Root leak #2 is tool-instance sharing:

- parent builds `TodoWriteTool(tracker=parent_tracker)`
- child does `parent_tools.filtered_copy(...)`
- child todo writes mutate `parent_tracker._todos`

Required fix in `run_subagent()` after the child registry is created:

1. Create a child-local tracker for todos, e.g. `child_todo_tracker = TaskTracker()`.
2. Replace the child registry's `todo` tool with a **plugin-wrapped** instance that uses the child tracker.
3. Keep using the parent `tracker` argument only for worker-task status (`start` / `update` / `finish`), not for todo item storage.

Plugin-aware replace is mandatory because the parent registry stores `AgentToolPlugin` wrappers, not bare `TodoWriteTool` instances:

```python
child_todo_tracker = TaskTracker()
todo_tool = TodoWriteTool(tracker=child_todo_tracker)
# Preserve the same plugin/runtime binding pattern used by parent tools.
# If the copied instance is AgentToolPlugin, replace with AgentToolPlugin(todo_tool, runtime=...).
# Do not install a bare TodoWriteTool into a registry that expects ToolPlugin wrappers.
agent_tools.replace("todo", wrapped_todo_plugin, wrapped_todo_plugin.description, wrapped_todo_plugin.parameters_schema())
```

Notes:

- Do not clear or rewrite parent tracker todos when a child starts/finishes.
- Child `todo` read/update/write must only see child-local items.
- Parent later `todo` read/update/write must continue from the pre-child parent list.
- Preserve tool schema/description and runtime binding so child execution context still works.

### 4. Gateway / frontend behavior

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

### 5. Invariants

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

## File Changes

| File | Responsibility |
|------|----------------|
| `src/voidx/presentation/output/events/consumers.py` | Branch on `agent_id`; upsert/clear subagent todo node; keep global pin main-only |
| `src/voidx/presentation/output/dock/app.py` | Optional helper to upsert/clear a non-pinned todo node under a parent |
| `src/voidx/agent/infrastructure/langgraph/execution.py` | Stop passing parent `todo_state_sink` |
| `src/voidx/agent/infrastructure/langgraph/runtime/subagent.py` | Install plugin-wrapped child-local todo tracker; keep local todo state + UI event |
| `src/voidx/presentation/gateway/adapter.py` | Hard-skip global todo pin notifications for `agent_id >= 0` |
| `src/tests/test_presentation/gateway/test_ui_events_todo.py` | Rewrite agent_id isolation expectations; empty-write and multi-child cases |
| `src/tests/test_presentation/gateway/test_adapter.py` | Prove child todo events do not emit global pin items |
| `src/tests/test_infrastructure/runtime/test_subagent_step_budget.py` | Stop requiring parent sink mutation if present; assert isolation |
| `src/tests/test_infrastructure/runtime/test_todo_events.py` | Cover child todo event still emitted with agent_id, without parent pin side effects where relevant |

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

### Runtime isolation

Add focused coverage around `run_subagent()`:

1. Parent tracker starts with todos `A`.
2. Child executes todo write with todos `B`.
3. Parent tracker still has `A`.
4. Parent task state todo remains `A` / previous parent value.
5. Child UI event is still produced with `agent_id >= 0` when events are enabled.
6. Child empty write clears only child-local tracker/state, not parent.
7. Two concurrent children writing different todo lists do not cross-contaminate parent or each other.

If existing sink tests currently assert parent mutation, invert them to assert non-mutation.

### Gateway

Add:

- child `TodoUpdated(agent_id>=0)` does not emit a global todo pin notification
- parent `TodoUpdated(agent_id<0)` still emits the existing todo pin notification

### Commands

```bash
./test.py --backend -- src/tests/test_presentation/gateway/test_ui_events_todo.py -q
./test.py --backend -- src/tests/test_presentation/gateway/test_adapter.py -k todo -q
./test.py --backend -- src/tests/test_infrastructure/runtime/test_todo_events.py -q
./test.py --backend -- src/tests/test_infrastructure/runtime/test_subagent_step_budget.py -q
./test.py --backend -- tui/tests/test_terminal_input.py -k todo -q
```

## Risks

- Existing test `test_todo_updated_with_agent_id_updates_global_root_todo` encodes the old shared-pin semantics and is a blocking rewrite.
- Shallow tool-registry copy means tracker isolation is mandatory; UI-only filtering is insufficient.
- Child todo tool replacement must preserve `AgentToolPlugin` wrapping/runtime binding or child todo execution can break.
- Child todo nodes under subagent trees may increase vertical transcript noise for chatty reviewers; acceptable because the alternative pollutes the main pin.
- If gateway still emits child todo pin notifications, web/desktop will keep the screenshot bug even after TUI is fixed.

## Rollback

Revert the consumer branch, restore parent `todo_state_sink`, and restore shared `TodoWriteTool` tracker usage. No schema migration is involved.

## Acceptance Criteria

1. Reproducing the screenshot scenario no longer shows child todos in the bottom `Todo:` pin.
2. Child todos appear under the child agent node in TUI transcript output.
3. Parent todo pin and parent runtime todo state survive concurrent child todo updates.
4. Child empty todo write clears only the child todo node / child-local state.
5. Gateway does not emit global todo pin notifications for child agents.
6. Focused backend tests above are green.
