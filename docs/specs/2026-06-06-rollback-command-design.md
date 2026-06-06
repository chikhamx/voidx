# `/rollback` Slash Command Design

> **Status: Draft**

## Problem

`SessionChangeTracker.rollback_current()` exists and works — it restores modified files and deletes newly created ones — but there is **no user-facing entry point**. Users have no way to undo file changes made by the agent in the current turn.

## Goal

Add a `/rollback` slash command that lets users revert all file changes from the most recent agent turn.

## Current Architecture

```
SessionChangeTracker
├── begin_turn()          — clears snapshots, starts tracking
├── capture_file()        — snapshots file before modification
├── capture_tool_call()   — auto-captures write/edit/lsp_format
├── record_diff()         — accumulates +/− line counts
├── finish_turn()         — marks changes as visible
├── rollback_current()    — restores files, returns RollbackResult
├── change_summary_lines()— formatted list of changed files
├── has_changes           — bool, True if turn has file changes
└── clear()               — discards all tracking state
```

Lifecycle: `begin_turn` → `capture_*` / `record_diff` → `finish_turn` → (optional `rollback_current`) → next `begin_turn` clears everything.

## Design

### Command: `/rollback`

**Behavior:**

1. Check `session_tracker.has_changes`. If no changes, print a message and return.
2. Show the pending changes (reuse `change_summary_lines()` output) and ask for confirmation.
3. On confirm, call `session_tracker.rollback_current()`.
4. Display the result: which files were restored, which were removed, any errors.

**No-arg variant only.** No `/rollback <turn-number>` — we only track the current turn.

### Confirmation Flow

Since `/rollback` is destructive, add a confirmation prompt:

```
/rollback
  Modified  auth.py  +12 −3
  Created   new_api.py  +45 −0

Rollback these changes? [y/N]
```

If the TUI app is available, use the existing `_select_from_list` pattern for a yes/no picker. Otherwise fall back to a simple text prompt.

### Edge Cases

| Case | Behavior |
|------|----------|
| No changes in current turn | Print "No file changes to roll back." and return |
| Agent is currently running | Reject: print "Cannot rollback while agent is busy." |
| Partial failure (some files fail to restore) | Report errors, keep snapshots (current behavior of `rollback_current`) |
| File was modified by user after agent changed it | Overwrite with snapshot — same as current behavior, warn in output |
| `/rollback` called twice | Second call: no changes (snapshots cleared after successful rollback) |

### Agent-Busy Guard

`/rollback` must not run while the agent is actively executing tools, because:
- The agent may be mid-write, leading to race conditions
- Snapshots are being mutated during tool execution

Check: if `task_state` indicates the agent is running, reject the command.

## Implementation Plan

### 1. Add `_rollback` method to `SlashSessionMixin`

File: `src/voidx/agent/slash/session.py`

```python
async def _rollback(self) -> None:
    if not session_tracker.has_changes:
        ui.print("[dim]No file changes to roll back.[/dim]")
        return

    lines = session_tracker.change_summary_lines()
    ui.print("[bold]Files changed this turn:[/bold]")
    for line in lines:
        ui.print(line)
    ui.print("")
    ui.print("Rollback these changes? [y/N]")

    # TODO: confirmation via TUI picker or text input
    # For now, require explicit "y"
    ...
    
    result = session_tracker.rollback_current()
    if result.ok:
        if result.restored:
            ui.print(f"[green]Restored:[/green] {', '.join(result.restored)}")
        if result.removed:
            ui.print(f"[green]Removed:[/green] {', '.join(result.removed)}")
    else:
        for err in result.errors:
            ui.error(err)
```

### 2. Register `/rollback` in `SlashHandler.dispatch`

File: `src/voidx/agent/slash/handler.py`

Add to the `handlers` dict:
```python
"/rollback": self._rollback,
```

### 3. Add to command palette

File: `src/voidx/ui/commands.py`

Add entry:
```python
("/rollback", "Revert file changes from the current turn"),
```

### 4. Add agent-busy guard

In `_rollback`, check task state before proceeding. The `SlashHandler` already has `_host_task_state()` access.

### 5. Tests

File: `tests/test_ui_session_changes.py` (extend existing)

- Test `/rollback` dispatch routes to `_rollback`
- Test `_rollback` with no changes → "no changes" message
- Test `_rollback` with changes → confirmation → rollback executed
- Test `_rollback` while agent busy → rejection

## Files Changed

| File | Change |
|------|--------|
| `src/voidx/agent/slash/session.py` | Add `_rollback()` method |
| `src/voidx/agent/slash/handler.py` | Register `/rollback` in dispatch |
| `src/voidx/ui/commands.py` | Add command palette entry |
| `tests/test_ui_session_changes.py` | Add rollback command tests |

## Out of Scope

- Multi-turn rollback (history stack) — significant complexity, separate design
- Selective file rollback (`/rollback auth.py`) — requires per-file snapshot selection
- Git-based rollback (`git checkout`) — different mechanism, separate command
