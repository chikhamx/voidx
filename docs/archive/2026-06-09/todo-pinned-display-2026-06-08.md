# TODO Pinned Display Phase B Design

> **Status: Done**

Phase A moved TODO updates to a single root-level transcript node. That solved duplicate TODO blocks, but it did not make TODO visually fixed because root tree content is still part of transcript rendering and can be flushed to terminal scrollback. Phase B makes TODO a separate pinned display state in the TUI.

## Goal

Keep the current TODO list visible near the input area while the agent is running, without depending on transcript tree position or scrollback flush behavior.

The TODO display should:

- stay fixed above the input box while transcript content scrolls or flushes;
- update from `TodoUpdated` events;
- not create extra transcript nodes;
- not block PureTui scrollback flushing;
- avoid worsening input typing, command palette, or choice prompt flicker.

## Non-goals

- Do not remove the Phase A root-level TODO transcript node in this phase.
- Do not change TODO tool semantics or payload shape.
- Do not add Web UI protocol changes unless a later Web UI design needs them.
- Do not make every transcript node sticky; this is TODO-specific.
- Do not redesign status bar, choice prompt, or input rendering.

## Current Constraints

PureTui renders three conceptual regions:

1. Transcript tail: `dock.tree.render(width)[committed:]`, limited by remaining height.
2. Optional UI chrome above/below input: panels, choice prompt, command palette.
3. Bottom region: input box and status lines.

`_flush_committed()` prints settled transcript lines to native scrollback, then active frame rendering only shows uncommitted tail content. This is why a root-level TODO node cannot be truly fixed: once it is settled, it may be flushed; if it is never settled, it blocks flush.

Input-only rendering is also sensitive:

- `_render_input_region()` captures only `_render_bottom_impl()`.
- choice selection can update only bottom rows when row count is unchanged.
- any pinned TODO solution that changes bottom row count while typing or moving selection will force full-frame redraws and can reintroduce flicker.

## Chosen Approach

Use a separate `DockTodoState` owned by `BottomInputDock`, and render it as a fixed frame section in PureTui above the normal bottom/input region.

This keeps TODO out of the transcript flush path while preserving Phase A's root TODO node for transcript history, Web transcript conversion, and restore/hydration.

### Data Model

Add a small structured state object, likely in `src/voidx/ui/output/dock/todo.py`:

```python
@dataclass(frozen=True)
class DockTodoItem:
    content: str
    status: str

@dataclass(frozen=True)
class DockTodoState:
    summary: str
    items: tuple[DockTodoItem, ...]
```

`BottomInputDock` owns:

- `_todo_state: DockTodoState | None`
- `set_todo_state(summary: str, items: Sequence[...]) -> None`
- `clear_todo_state() -> None`
- `todo_state() -> DockTodoState | None`

`reset()` clears the pinned TODO state. `restore_tree()` should hydrate `_todo_state` from the latest root-level TODO node payload if present, so resumed sessions can show the current TODO list without waiting for another `TodoUpdated` event.

### Event Flow

`DockEventConsumer._update_todo_node()` continues to update the Phase A root-level TODO node, then also calls `dock.set_todo_state(...)` with the same structured data.

The transcript node remains settled, so it does not block flush. The pinned state becomes the source for fixed TUI display.

### Rendering Flow

PureTui should render pinned TODO as a separate fixed section:

```text
<transcript tail>
Todo: 1/4 done · 1 active · 2 pending
  ◐ implement pinned display
  ○ add tests
  ○ verify flicker
────────────────
❯ input
────────────────
status bar
```

The pinned TODO section is inserted in `_render_impl()` after transcript elements and before `_render_bottom_elements(...)`.

It should not be part of `_render_bottom_impl()`. That keeps typing and choice navigation on the existing partial-render path: input-only redraws rewrite the bottom/input region, while the pinned TODO section remains untouched unless TODO state changes or a full frame redraw is required.

`fixed_lines` in `_render_impl()` must include the rendered pinned TODO row count, so the transcript tail never overlaps the pinned section.

Cursor positioning does not need to count pinned TODO rows because the cursor movement is calculated from the bottom/input region upward; pinned TODO sits above that region.

### Compact Display Rules

Pinned TODO should be intentionally compact:

- summary line is always shown when TODO state exists;
- show at most 4 TODO items by default;
- order items by `in_progress`, `pending`, `completed`, `cancelled`;
- show an omitted-count line when hidden items remain;
- clip long lines to terminal width using display-cell-aware clipping;
- on very small terminals, degrade to summary-only rather than consuming most of the screen.

Suggested icons can reuse Phase A:

- pending: `○`
- in progress: `◐`
- completed: `●`
- cancelled: `✕`

### Restore Behavior

On `restore_tree(tree)`, scan root children from newest to oldest for `node_type == "todo"` with payload:

```json
{
  "summary": "...",
  "items": [{"content": "...", "status": "..."}]
}
```

If valid, hydrate `_todo_state`. If missing or malformed, leave `_todo_state = None`.

This keeps restore best-effort and avoids coupling pinned display to historical transcript parsing.

### Clear Behavior

`reset()` and `/clear` should clear `_todo_state` together with the tree. A new session should not inherit a previous session's pinned TODO.

## Implementation Plan

1. Add `DockTodoItem` and `DockTodoState`.
2. Add TODO state methods to `BottomInputDock`.
3. Update `DockEventConsumer._update_todo_node()` to set pinned TODO state after updating the root transcript node.
4. Add restore hydration from root TODO payload.
5. Add PureTui rendering helpers:
   - `_render_pinned_todo_elements(width) -> list[Text]`
   - `_pinned_todo_row_count(width) -> int`
6. Insert pinned TODO elements into `_render_impl()` before bottom elements.
7. Keep `_render_bottom_impl()` unchanged so input-only and choice-selection-only redraw paths remain stable.

## Testing Plan

### `tests/test_ui_events.py`

- `test_todo_updated_sets_pinned_todo_state`
  - `TodoUpdated` updates root transcript TODO and dock pinned TODO state.
- `test_restore_tree_hydrates_pinned_todo_state`
  - restoring a tree with root TODO payload populates dock TODO state.
- `test_reset_clears_pinned_todo_state`
  - clearing/resetting the dock removes pinned TODO state.

### `tests/test_pure_tui.py`

- `test_pinned_todo_renders_above_input_and_status`
  - rendered frame order is transcript, pinned TODO, input box, status.
- `test_pinned_todo_reduces_transcript_body_limit`
  - transcript tail does not overlap pinned TODO on small heights.
- `test_pinned_todo_shows_four_items_when_row_budget_allows`
  - a 5-row TODO budget renders summary plus 4 visible items.
- `test_pinned_todo_not_in_bottom_impl`
  - `_render_bottom_impl()` does not include TODO text, preserving input-only redraw scope.
- `test_input_region_render_still_uses_bottom_only_with_pinned_todo`
  - typing with stable TODO state does not force full frame redraw.
- `test_choice_selection_only_render_still_works_with_pinned_todo`
  - moving choice selection can still use selection-only rendering when row count is stable.
- `test_pinned_todo_summary_only_on_tiny_height_or_width`
  - compact fallback prevents TODO from consuming the entire viewport.

## Acceptance Criteria

- TODO is visible in the fixed frame section above input while transcript content scrolls/flushes.
- `safe_flush_line_count()` behavior is unchanged by pinned TODO display.
- Repeated `TodoUpdated` events update the pinned display without creating duplicate transcript TODO nodes.
- `/clear` removes the pinned TODO display.
- Restoring a session with a valid root TODO payload hydrates the pinned display.
- Typing in the input box does not redraw transcript or pinned TODO rows.
- Choice selection movement does not clear the rest of the screen when row count is stable.
