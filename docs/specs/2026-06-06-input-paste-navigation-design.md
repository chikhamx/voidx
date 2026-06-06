# Input Box Paste & Navigation Improvements

> **Status: Draft**

## Problem

Four UX issues in the TUI input box:

### 1. Long pasted text floods the input box

When a user pastes a large block of text (e.g. a stack trace, log output, code snippet), every line is inserted as a real editor line. A 50-line paste creates 50 input lines, pushing the status bar off-screen and making the input area unusable. The user can't see what they pasted or easily edit it.

**Current behavior**: `_insert_pasted_text()` splits on `\n` and calls `_insert_newline()` + `_insert_text()` for each line. The full text is stored in `_input_lines` and rendered verbatim.

**Desired behavior**: Like Claude Code, collapse the pasted text into a compact token like `[Pasted text #1 +34 lines]` that the user can see at a glance. The full text is preserved internally and expanded into the message on submit.

### 2. Arrow Up in multiline input jumps to history

When the user has multiple input lines and presses ↑ to move the cursor up one line, `_history_prev()` fires instead. The cursor should move up within the editor first, and only fall through to history when the cursor is already on the first line at column 0.

**Current behavior** (`parser.py:304-312`):
```python
if final == 0x41:  # Up
    if self._active_choice is not None:
        self._move_choice(-1)
    elif self._attachment_panel_active():
        self._move_attachment_selection(-1)
    elif self._command_panel_active:
        self._move_command_selection(-1)
    else:
        self._history_prev()
```

No check for whether the cursor can move up within the editor.

**Desired behavior**: If `_cursor_row > 0`, move cursor up one row (keeping column). Only call `_history_prev()` when `_cursor_row == 0`. Same logic for ↓: if `_cursor_row < len(_input_lines) - 1`, move down; otherwise call `_history_next()`.

### 3. Pasted images lack a visible token in the input

When the user pastes an image (Ctrl+V or `/paste`), the image is saved to `.voidx/attachments/` and a token like `[image-clipboard-20260606-abc12345]` is inserted. This token is functional but not user-friendly — it doesn't convey that an image was pasted or show its size.

**Desired behavior**: Display a compact, descriptive token like `[Pasted image #1 142KB]` in the input box. The underlying `[image-stem]` token is still used for message processing, but the display is human-readable.

### 4. Pasted content display anomalies in the main conversation

After submitting, the pasted text/image should render properly in the conversation transcript. Currently there are anomalies — the display may show raw tokens, truncated content, or missing attachment indicators. This needs investigation and fixing.

## Design

### 1. Pasted Text Collapsing

#### Data Model

Add a paste registry to `PureTui`:

```python
# app.py — new state
self._paste_registry: list[dict] = []
# Each entry: {"id": int, "text": str, "lines": int, "token": str}
```

Add a display-only token format:

```python
_PASTE_TOKEN_RE = re.compile(r'\[Pasted text #(\d+)(?: \+(\d+) lines)?\]')
```

#### Insertion Flow

When `_insert_pasted_text()` receives text with > 3 lines (or > 200 characters), instead of inserting all lines:

1. Register the full text in `_paste_registry` with an incrementing ID.
2. Count lines beyond the first: `extra = total_lines - 1`.
3. Insert a single token line: `[Pasted text #N +M lines]` (or `[Pasted text #N]` if single long line).
4. The token is stored as a regular line in `_input_lines`.

Short pastes (≤ 3 lines and ≤ 200 chars) are inserted verbatim as before — no collapsing.

#### Token Rendering

The token line is rendered with a distinct style in the input box:

```
❯ [Pasted text #1 +34 lines]     ← dim cyan, visually distinct
```

In `_input_display_text()`, detect paste tokens and apply a special style. The cursor can be positioned on the token line but editing it replaces the entire paste (delete the token, insert new text).

#### Submit Expansion

On submit (`_do_submit`), before queuing the text, expand all paste tokens back to their original text:

```python
def _expand_paste_tokens(self, text: str) -> str:
    def _replacer(match):
        paste_id = int(match.group(1))
        for entry in self._paste_registry:
            if entry["id"] == paste_id:
                return entry["text"]
        return match.group(0)
    return _PASTE_TOKEN_RE.sub(_replacer, text)
```

The expanded text is what gets queued to `_queue` and processed by the agent.

#### Paste Token Editing

If the user positions the cursor on a paste token line and types, the token line is replaced with the new input (the pasted content is lost from that line). Backspace on a token line deletes the entire token at once.

This keeps the interaction simple: the token is a preview, not an editable region.

#### Cleanup

`_paste_registry` entries are cleared when the input is cleared (`_clear_input`) or when the text is submitted.

### 2. Arrow Key Navigation in Multiline Input

#### Change

In `_dispatch_csi()` (parser.py), replace the direct `_history_prev()` / `_history_next()` calls with cursor-aware logic:

```python
if final == 0x41:  # Up
    if self._active_choice is not None:
        self._move_choice(-1)
    elif self._attachment_panel_active():
        self._move_attachment_selection(-1)
    elif self._command_panel_active:
        self._move_command_selection(-1)
    elif self._cursor_row > 0:
        self._cursor_row -= 1
        self._clamp_cursor_col()
        self._update_input_panels()
    else:
        self._history_prev()
    return (consumed, None)

if final == 0x42:  # Down
    if self._active_choice is not None:
        self._move_choice(1)
    elif self._attachment_panel_active():
        self._move_attachment_selection(1)
    elif self._command_panel_active:
        self._move_command_selection(1)
    elif self._cursor_row < len(self._input_lines) - 1:
        self._cursor_row += 1
        self._clamp_cursor_col()
        self._update_input_panels()
    else:
        self._history_next()
    return (consumed, None)
```

Add a helper to clamp column to the target line length:

```python
def _clamp_cursor_col(self) -> None:
    line = self._current_line()
    self._cursor_col = min(self._cursor_col, len(line))
```

This preserves the column position when moving between lines of similar length, and clamps when the target line is shorter — matching standard editor behavior.

### 3. Image Paste Token Display

#### Current Flow

1. User presses Ctrl+V → `_paste_clipboard_image_quiet()`
2. Image saved to `.voidx/attachments/clipboard-TIMESTAMP-RAND.png`
3. Token `[image-clipboard-TIMESTAMP-RAND]` inserted via `_insert_text_token()`
4. `_notice` set to message like `"Pasted image: .voidx/attachments/clipboard-...png (142KB)"`

#### Change

Replace the raw `[image-stem]` token with a user-friendly display token in the input box, while keeping the internal token for message processing.

Add a paste-image registry similar to the text paste registry:

```python
self._image_registry: list[dict] = []
# Each entry: {"id": int, "stem": str, "display": str, "size_text": str}
```

When inserting an image:

1. Register the image in `_image_registry` with incrementing ID.
2. Insert display token: `[Pasted image #N SIZE]` (e.g. `[Pasted image #1 142KB]`).
3. On submit, expand `[Pasted image #N SIZE]` → `[image-stem]` before queuing.

The `_ATTACHMENT_RE` regex in `attachments.py` already matches `[image-stem]`, so the expansion target stays compatible.

### 4. Pasted Content Display in Conversation

#### Investigation Needed

The issue is that pasted content (both text and images) may not render correctly in the main conversation transcript after submission. Possible causes:

1. **Paste tokens not expanded before display**: If `_do_submit()` queues the raw token text without expansion, the conversation shows `[Pasted text #1 +34 lines]` instead of the actual content.
2. **Image tokens not expanded**: Same issue — `[Pasted image #1 142KB]` reaches the agent instead of `[image-stem]`.
3. **`display_text` truncation**: `start_turn()` in `dock/app.py:152` truncates to 160 chars: `preview = _clean(text)[:160]`. For long pastes, the display is cut off without indicating there's more.
4. **Attachment display**: `_display_text()` in `attachments.py:299` appends `[attachments: ...]` but doesn't handle paste tokens.

#### Fix Plan

1. Ensure `_do_submit()` calls `_expand_paste_tokens()` and `_expand_image_tokens()` before queuing.
2. In `start_turn()`, improve preview for long content: show first line + `... (+N more lines)` indicator.
3. Verify the full expansion pipeline: input token → submit expansion → `build_user_message_payload()` → attachment parsing → display.

## Implementation Plan

| Step | Description | Files |
|------|-------------|-------|
| 1 | Add paste registry, `_insert_pasted_text()` collapsing, `_expand_paste_tokens()` | `src/voidx/ui/tui/app.py`, `src/voidx/ui/tui/parser.py`, `src/voidx/ui/tui/input.py` |
| 2 | Arrow key navigation: cursor-aware up/down with `_clamp_cursor_col()` | `src/voidx/ui/tui/parser.py`, `src/voidx/ui/tui/input.py` |
| 3 | Image paste token display: image registry, display tokens, expansion | `src/voidx/ui/tui/clipboard_mixin.py`, `src/voidx/ui/tui/app.py`, `src/voidx/ui/tui/input.py` |
| 4 | Fix pasted content display in conversation: submit expansion, preview improvement | `src/voidx/ui/tui/app.py`, `src/voidx/ui/output/dock/app.py` |
| 5 | Paste token rendering style in input box | `src/voidx/ui/tui/renderer.py` |
| 6 | Tests | `tests/test_pure_tui.py` |

## Out of Scope

- Editing inside a collapsed paste token (expand-on-edit is a future enhancement)
- Paste token persistence across sessions
- Drag-and-drop file support
- Web UI / desktop UI paste handling (TUI-only for now)
