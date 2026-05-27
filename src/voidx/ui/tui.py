"""TUI — live-filtering slash input. Hints below prompt, no mode switch."""

from __future__ import annotations

import sys
import shutil
import unicodedata

from voidx.ui.keys import (
    Key,
    enable_bracketed_paste,
    disable_bracketed_paste,
    read_key,
)

MAX_PASTE_CHARS = 500


def _w(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def _display_width(text: str) -> int:
    """Return terminal display width of *text*.
    Characters with East Asian Width 'W' or 'F' count as 2 columns;
    everything else counts as 1 column."""
    return sum(
        2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        for ch in text
    )


def _visual_rows(text: str, prefix_width: int, term_width: int) -> int:
    dw = prefix_width + _display_width(text)
    if dw <= 0:
        return 1
    return (dw - 1) // term_width + 1


# ── live_input ──────────────────────────────────────────────────────────

def live_input(commands: list[tuple[str, str]]) -> str:
    """Read a line of input. When text starts with /, show matching
    commands as dim hints below the prompt. No mode switching.

    - /asd   → (no matches) → plain text, hints auto-hide
    - /cl    → shows matching hints, Enter if exact match
    - hello  → normal input, no hints
    - Esc    → clear line
    - Enter  → return text (or selected command name if slash-mode + selected)
    - UP/DOWN → navigate hint matches
    - TAB    → autocomplete to selected command

    Paste (bracketed paste mode):
    - Pasted text is inserted into buffer instead of being sent immediately
    - Newlines in pasted text are preserved (multi-line display)
    - Pasted text exceeding MAX_PASTE_CHARS is displayed truncated but sent in full
    """
    buffer: list[str] = []
    cursor_pos = 0
    hint_count = 0
    prev_visual = 0
    cursor_visual_row = 0
    selected_index = -1
    paste_mode = False
    _skip_next_lf = False       # Windows: skip \n after \r during paste
    _paste_full_text: str | None = None  # full pasted text when display truncated

    enable_bracketed_paste()

    def _get_matches() -> list[tuple[str, str]]:
        """Return filtered [(name, desc), …] matching current buffer."""
        prefix = "".join(buffer)
        if not prefix.startswith("/"):
            return []
        p = prefix.lower()
        return [(n, d) for n, d in commands if n.lower().startswith(p)]

    def _render():
        nonlocal hint_count, selected_index, prev_visual, cursor_visual_row
        out: list[str] = []

        _prev_cursor_row = cursor_visual_row
        if _prev_cursor_row > 0:
            out.append(f"\x1b[{_prev_cursor_row}A")

        text = "".join(buffer)
        lines = text.split("\n")
        term_width = shutil.get_terminal_size().columns or 80

        visual_row_counts = []
        for line in lines:
            visual_row_counts.append(_visual_rows(line, 2, term_width))

        out.append("\r\x1b[K❯ " + lines[0])
        for line in lines[1:]:
            out.append("\n\r\x1b[K  " + line)

        prefix = "".join(buffer)
        new_hint_count = 0
        hint_visual_rows = 0
        if prefix.startswith("/"):
            p = prefix.lower()
            matches = [(n, d) for n, d in commands if n.lower().startswith(p)]
            if matches:
                limit = 5 if prefix == "/" else 6
                if selected_index >= len(matches):
                    selected_index = -1
                scroll_start = 0
                if selected_index >= 0:
                    if selected_index >= scroll_start + limit:
                        scroll_start = selected_index - limit + 1
                    if selected_index < scroll_start:
                        scroll_start = selected_index
                for i, (name, desc) in enumerate(matches[scroll_start : scroll_start + limit]):
                    out.append("\n")
                    if i + scroll_start == selected_index:
                        out.append(f"\r\x1b[K  \x1b[34;1m{name}\x1b[0m  {desc}")
                    else:
                        out.append(f"\r\x1b[K  \x1b[2m{name}\x1b[0m  {desc}")
                    new_hint_count += 1
                    hint_visual_rows += _visual_rows(f"{name}  {desc}", 2, term_width)
            else:
                selected_index = -1

        new_visual = sum(visual_row_counts) + hint_visual_rows
        for _ in range(new_visual, prev_visual):
            out.append("\n\r\x1b[K")
        prev_visual = new_visual
        hint_count = new_hint_count

        char_count = 0
        cursor_visual_row = 0
        for i, line in enumerate(lines):
            line_len = len(line)
            if cursor_pos <= char_count + line_len:
                chars_before = cursor_pos - char_count
                text_before = line[:chars_before]
                dw = 2 + _display_width(text_before)
                cursor_visual_row = sum(visual_row_counts[:i]) + dw // term_width
                cursor_col = (dw % term_width) + 1
                break
            char_count += line_len + 1

        back = max(new_visual - 1, 0)
        if back:
            out.append(f"\x1b[{back}A")
        if cursor_visual_row > 0:
            out.append(f"\x1b[{cursor_visual_row}B")
        out.append(f"\x1b[{cursor_col}G")

        _w("".join(out))

    def _truncate_paste() -> None:
        """If pasted text exceeds limit, replace buffer with summary display
        but preserve full text for submission."""
        nonlocal _paste_full_text
        text = "".join(buffer)
        if len(text) > MAX_PASTE_CHARS:
            _paste_full_text = text
            lines = text.split("\n")
            first_line = lines[0]
            if len(first_line) > 80:
                first_line = first_line[:77] + "..."
            summary = f"[Pasted {len(text)} chars, {len(lines)} lines] {first_line}"
            buffer[:] = list(summary)
        else:
            _paste_full_text = None

    def _maybe_clear_full_text() -> None:
        """If user edits a truncated paste, discard the full text."""
        nonlocal _paste_full_text
        if _paste_full_text is not None and not paste_mode:
            _paste_full_text = None

    _render()

    try:
        while True:
            ev = read_key()

            # ── Bracketed paste start ─────────────────────────────────
            if ev.is_paste_start:
                paste_mode = True
                _skip_next_lf = False
                selected_index = -1
                continue

            # ── Bracketed paste end ───────────────────────────────────
            if ev.is_paste_end:
                paste_mode = False
                _skip_next_lf = False
                selected_index = -1
                _truncate_paste()
                _render()
                continue

            # ── UP/DOWN/TAB navigation (only outside paste mode) ──────
            if ev.key == Key.UP:
                _maybe_clear_full_text()
                if not paste_mode:
                    prefix = "".join(buffer)
                    if prefix.startswith("/"):
                        matches = _get_matches()
                        if matches:
                            if selected_index > 0:
                                selected_index -= 1
                            else:
                                selected_index = len(matches) - 1
                            _render()

            elif ev.key == Key.DOWN:
                _maybe_clear_full_text()
                if not paste_mode:
                    prefix = "".join(buffer)
                    if prefix.startswith("/"):
                        matches = _get_matches()
                        if matches:
                            if selected_index < len(matches) - 1:
                                selected_index += 1
                            else:
                                selected_index = 0
                            _render()

            elif ev.key == Key.TAB:
                _maybe_clear_full_text()
                if not paste_mode and selected_index >= 0:
                    matches = _get_matches()
                    if selected_index < len(matches):
                        name = matches[selected_index][0]
                        buffer[:] = list(name)
                        cursor_pos = len(buffer)
                        selected_index = -1
                        _render()

            # ── ENTER ─────────────────────────────────────────────────
            elif ev.key == Key.ENTER:
                if paste_mode:
                    # During paste, Enter is a newline from \r
                    buffer.insert(cursor_pos, "\n")
                    cursor_pos += 1
                    _skip_next_lf = True
                    _render()
                elif selected_index >= 0:
                    matches = _get_matches()
                    if selected_index < len(matches):
                        name = matches[selected_index][0]
                        buffer[:] = list(name)
                        cursor_pos = len(buffer)
                        selected_index = -1
                        _render()
                else:
                    _w(f"\x1b[{cursor_visual_row}A")
                    _w("\r\x1b[J")
                    disable_bracketed_paste()
                    if _paste_full_text is not None:
                        return _paste_full_text
                    return "".join(buffer)

            elif ev.key == Key.ESC:
                _maybe_clear_full_text()
                buffer.clear()
                cursor_pos = 0
                selected_index = -1
                _render()

            elif ev.key == Key.LEFT:
                _maybe_clear_full_text()
                if cursor_pos > 0:
                    cursor_pos -= 1
                    selected_index = -1
                    _render()

            elif ev.key == Key.RIGHT:
                _maybe_clear_full_text()
                if cursor_pos < len(buffer):
                    cursor_pos += 1
                    selected_index = -1
                    _render()

            elif ev.key == Key.BACKSPACE:
                _maybe_clear_full_text()
                if cursor_pos > 0:
                    buffer.pop(cursor_pos - 1)
                    cursor_pos -= 1
                    selected_index = -1
                    _render()

            elif ev.key == Key.CTRL_C:
                _w("^C\n")
                disable_bracketed_paste()
                raise KeyboardInterrupt()

            elif ev.key == Key.CTRL_D and not buffer:
                _w("\n")
                disable_bracketed_paste()
                raise EOFError()

            elif ev.is_char:
                ch = ev.char

                if paste_mode:
                    if ch == "\n":
                        if _skip_next_lf:
                            _skip_next_lf = False
                            continue
                        buffer.insert(cursor_pos, "\n")
                        cursor_pos += 1
                    elif ch == "\r":
                        buffer.insert(cursor_pos, "\n")
                        cursor_pos += 1
                        _skip_next_lf = True
                    elif ch.isprintable():
                        _maybe_clear_full_text()
                        buffer.insert(cursor_pos, ch)
                        cursor_pos += 1
                    else:
                        continue
                elif ch.isprintable():
                    _maybe_clear_full_text()
                    buffer.insert(cursor_pos, ch)
                    cursor_pos += 1
                else:
                    continue

                selected_index = -1
                _render()

    finally:
        disable_bracketed_paste()
        # Also restore on unexpected exit paths
        try:
            disable_bracketed_paste()
        except Exception:
            pass


# ── live_choice ─────────────────────────────────────────────────────────

def live_choice(prompt: str, choices: list[tuple[str, str, str]]) -> str | None:
    """Interactive choice menu — same visual style as / command hints.

    Renders choices below the current cursor line.
    UP/DOWN to navigate, Enter to confirm, ESC to cancel.
    Single-character values can be typed directly as quick keys
    (e.g. 'a', 'y', 'n').

    Args:
        prompt: Question shown above choices (caller should print this first).
        choices: List of (label, value, description) tuples.
                 label is the display name (e.g. "Always")
                 value is what gets returned on confirm (e.g. "a")
                 description is shown dimmed next to label.

    Returns:
        value of the selected choice, or None if ESC/cancelled.
    """
    if not choices:
        return None

    choice_count = len(choices)
    selected_index = 0

    # Map single-char values for quick-key lookup
    quick_keys: dict[str, str] = {v: v for _, v, _ in choices if len(v) == 1}

    def _render() -> None:
        out: list[str] = []
        for i, (label, value, desc) in enumerate(choices):
            out.append("\n")
            if i == selected_index:
                out.append(f"\r\x1b[K  \x1b[34;1m{label}\x1b[0m  {desc}")
            else:
                out.append(f"\r\x1b[K  \x1b[2m{label}\x1b[0m  {desc}")
        _w("".join(out))

    def _clear() -> None:
        """Clear all rendered choice lines from the terminal."""
        for _ in range(choice_count):
            _w("\n\r\x1b[K")
        if choice_count:
            _w(f"\x1b[{choice_count}A")

    _render()

    try:
        while True:
            ev = read_key()

            # ── Navigation ────────────────────────────────────────────────
            if ev.key == Key.UP:
                selected_index = (selected_index - 1) % choice_count
                _render()

            elif ev.key == Key.DOWN:
                selected_index = (selected_index + 1) % choice_count
                _render()

            # ── Confirm / Cancel ─────────────────────────────────────────
            elif ev.key == Key.ENTER:
                return choices[selected_index][1]

            elif ev.key == Key.ESC:
                return None

            elif ev.key == Key.CTRL_C:
                raise KeyboardInterrupt()

            # ── Quick key: type single-char value directly ───────────────
            elif ev.is_char and ev.char in quick_keys:
                return quick_keys[ev.char]

            # Ignore all other keys
            continue

    finally:
        _clear()
