# `/rollback` Slash Command Design

> **Status: Done**

## Problem

`SessionChangeTracker.rollback_current()` exists and works: it restores modified files from snapshots and deletes newly created files. There was no user-facing entry point, so users could not undo file changes made by the agent in the most recent completed turn.

## Goal

Add a no-argument `/rollback` slash command that reverts rollbackable file changes from the current tracked turn.

## Current Architecture

```text
SessionChangeTracker
├── begin_turn()                — clears snapshots, starts tracking
├── capture_file()              — snapshots file before modification
├── capture_tool_call()         — auto-captures write/edit/lsp_format/apply_patch
├── record_diff()               — accumulates +/− line counts for display
├── finish_turn()               — marks snapshots and diffs visible
├── rollback_current()          — restores files, returns RollbackResult
├── has_changes                 — visible diff records exist
├── has_rollbackable_changes    — visible snapshots exist
├── change_summary_lines()      — formatted diff-backed changed-file list
├── rollback_summary_lines()    — formatted rollback list, including snapshot-only files
└── clear()                     — discards all tracking state
```

Lifecycle: `begin_turn` -> `capture_*` / `record_diff` -> `finish_turn` -> optional `/rollback` -> next `begin_turn` clears remaining tracking state.

## Design

### Command: `/rollback`

Behavior:

1. Check `session_tracker.has_rollbackable_changes`. If no visible snapshots exist, print a message and return.
2. Show rollback candidates with `session_tracker.rollback_summary_lines()`.
3. Warn that rollback overwrites current file contents with the pre-edit snapshot.
4. Ask for confirmation.
5. On confirm, call `session_tracker.rollback_current()`.
6. Display restored files, removed files, and any errors.

No `/rollback <turn-number>` variant is included. Multi-turn rollback needs a history stack and is out of scope.

### Confirmation Flow

The command is destructive, so the default action must be cancel.

If a TUI app with `ask_choice()` is available, call it directly with `Cancel` first:

```python
choice = await app.ask_choice(
    "Rollback these changes?",
    [
        ("Cancel", "no", "Keep current files"),
        ("Rollback", "yes", "Restore captured snapshots"),
    ],
)
```

This avoids `_select_from_list()` accidentally defaulting to the destructive option. Without a TUI app, use the existing text prompt path and require explicit `y` / `yes`.

### Busy State

No explicit agent-busy guard is implemented in this design. Current slash dispatch is driven by the input queue and runs after the active turn yields control, while `TaskState` represents intent/approval state rather than runtime execution. If a future web or concurrent command path can invoke slash commands during tool execution, that path should provide a real runtime busy flag before enabling `/rollback`.

### Edge Cases

| Case | Behavior |
|------|----------|
| No rollbackable snapshots | Print "No file changes to roll back." and return |
| Snapshot exists but no diff record | Show the file as `(snapshot only)` and allow rollback |
| User cancels confirmation | Print "Rollback cancelled." and keep snapshots |
| Partial failure | Report errors and keep snapshots, matching `rollback_current()` behavior |
| File changed after the agent turn | Rollback overwrites current contents with the pre-edit snapshot; confirmation text warns about this |
| `/rollback` called twice after success | Second call prints no rollbackable changes because snapshots were cleared |

## Implementation Plan

### 1. Extend `SessionChangeTracker`

File: `src/voidx/ui/session.py`

- Add `has_rollbackable_changes`, based on visible snapshots.
- Add `rollback_summary_lines()`, reusing diff-backed summaries and adding snapshot-only entries.

### 2. Add `_rollback()` to `SlashSessionMixin`

File: `src/voidx/agent/slash/session.py`

```python
async def _rollback(self) -> None:
    if not session_tracker.has_rollbackable_changes:
        ui.print("[dim]No file changes to roll back.[/dim]")
        return

    lines = session_tracker.rollback_summary_lines()
    ...
    confirmed = await self._confirm_rollback()
    if not confirmed:
        ui.print("[dim]Rollback cancelled.[/dim]")
        return

    result = session_tracker.rollback_current()
    ...
```

### 3. Register `/rollback`

File: `src/voidx/agent/slash/handler.py`

Add to the `handlers` dict:

```python
"/rollback": self._rollback,
```

### 4. Add to Command Palette

File: `src/voidx/ui/commands.py`

```python
("/rollback", "Revert file changes from the current turn"),
```

### 5. Tests

Files:

- `tests/test_ui_session_changes.py`
- `tests/test_agent/test_slash_session.py`

Coverage:

- `has_rollbackable_changes` is true when snapshots exist even without diff records.
- `rollback_summary_lines()` includes snapshot-only files.
- `/rollback` dispatch restores and removes files after confirmation.
- `/rollback` cancellation leaves files and snapshots intact.
- `/rollback` with no snapshots prints the no-op message.
- Command palette contains `/rollback`.

## Files Changed

| File | Change |
|------|--------|
| `src/voidx/ui/session.py` | Add snapshot-based rollback availability and summary |
| `src/voidx/agent/slash/session.py` | Add `_rollback()` and confirmation helper |
| `src/voidx/agent/slash/handler.py` | Register `/rollback` in dispatch |
| `src/voidx/ui/commands.py` | Add command palette entry |
| `tests/test_ui_session_changes.py` | Add tracker-level rollback availability tests |
| `tests/test_agent/test_slash_session.py` | Add slash command behavior tests |

## Out of Scope

- Multi-turn rollback history
- Selective file rollback (`/rollback auth.py`)
- Git-based rollback (`git checkout` / `git restore`)
- True concurrent rollback while tools are executing
