"""Line editing — cursor movement, insert/delete, history."""

from __future__ import annotations


class _InputEditorMixin:
    """Methods: _current_line, _set_current_line, _insert_text, _delete_backward,
    _delete_forward, _insert_newline, _cursor_left, _cursor_right, _cursor_home,
    _cursor_end, _history_prev, _history_next, _load_history_item, _record_history,
    _is_input_empty, _get_input_text, _clear_input, _input_cursor_position,
    _set_input_text_and_cursor, _insert_text_token."""

    # ── line editing ─────────────────────────────────────────────────────

    def _current_line(self) -> str:
        return self._input_lines[self._cursor_row] if self._input_lines else ""

    def _set_current_line(self, text: str) -> None:
        if self._input_lines:
            self._input_lines[self._cursor_row] = text

    # ── paste token registry ─────────────────────────────────────────────

    def _paste_entries_snapshot(self) -> list[dict[str, object]]:
        return [dict(entry) for entry in getattr(self, "_paste_entries", [])]

    def _restore_paste_entries(self, entries: list[dict[str, object]]) -> None:
        self._paste_entries = [dict(entry) for entry in entries]
        max_id = 0
        for entry in self._paste_entries:
            try:
                max_id = max(max_id, int(entry.get("id", 0)))
            except (TypeError, ValueError):
                continue
        self._paste_next_id = max_id + 1

    def _clear_paste_entries(self) -> None:
        self._paste_entries = []
        self._paste_next_id = 1

    def _register_paste_entry(self, *, kind: str, display: str, expanded: str) -> str:
        paste_id = self._paste_next_id
        self._paste_next_id += 1
        entry = {
            "id": paste_id,
            "kind": kind,
            "display": display,
            "expanded": expanded,
        }
        self._paste_entries.append(entry)
        return display

    def _register_text_paste(self, text: str) -> str:
        paste_id = self._paste_next_id
        line_count = len(text.split("\n"))
        if line_count > 1:
            display = f"[Pasted text #{paste_id} +{line_count - 1} lines]"
        else:
            display = f"[Pasted text #{paste_id} {len(text)} chars]"
        return self._register_paste_entry(kind="text", display=display, expanded=text)

    def _register_image_paste(self, stem: str, size: int) -> str:
        from voidx.ui.tools.attachment_tokens import image_attachment_token_text

        paste_id = self._paste_next_id
        display = f"[Pasted image #{paste_id} {self._format_paste_size(size)}]"
        return self._register_paste_entry(
            kind="image",
            display=display,
            expanded=image_attachment_token_text(stem),
        )

    def _expand_registered_tokens(
        self,
        text: str,
        entries: list[dict[str, object]] | None = None,
    ) -> str:
        result = text
        for entry in entries if entries is not None else self._paste_entries:
            display = str(entry.get("display", ""))
            expanded = str(entry.get("expanded", ""))
            if display:
                result = result.replace(display, expanded)
        return result

    @staticmethod
    def _format_paste_size(size: int) -> str:
        if size < 1024:
            return f"{size}B"
        if size < 1024 * 1024:
            return f"{round(size / 1024)}KB"
        return f"{size / (1024 * 1024):.1f}MB"

    def _registered_paste_displays(self) -> list[tuple[str, str]]:
        displays = []
        for entry in getattr(self, "_paste_entries", []):
            display = str(entry.get("display", ""))
            kind = str(entry.get("kind", "text"))
            if display:
                displays.append((display, kind))
        displays.sort(key=lambda item: len(item[0]), reverse=True)
        return displays

    def _clear_attachment_suppression_on_edit(self) -> None:
        self._attachment_panel_suppressed_text = ""
        self._skill_panel_suppressed_text = ""

    def _insert_text(self, text: str) -> None:
        if self._active_choice is not None:
            if len(text) != 1 or not text.isascii():
                return
            quick = text.lower()
            for _, value, _ in self._active_choice:
                if len(value) == 1 and value.lower() == quick:
                    self._finish_choice(value)
                    self.invalidate()
                    return
            return

        self._reset_ctrl_c()
        line = self._current_line()
        col = min(self._cursor_col, len(line))
        new_line = line[:col] + text + line[col:]
        self._set_current_line(new_line)
        self._cursor_col = col + len(text)
        self._clear_attachment_suppression_on_edit()
        self._update_input_panels()

    def _delete_backward(self) -> None:
        if self._active_choice is not None:
            return
        self._reset_ctrl_c()
        if self._cursor_col > 0:
            line = self._current_line()
            col = self._cursor_col
            new_line = line[: col - 1] + line[col:]
            self._set_current_line(new_line)
            self._cursor_col -= 1
            self._clear_attachment_suppression_on_edit()
        elif self._cursor_row > 0:
            # Join with previous line
            prev_line = self._input_lines[self._cursor_row - 1]
            cur_line = self._current_line()
            new_cursor = len(prev_line)
            self._input_lines[self._cursor_row - 1] = prev_line + cur_line
            del self._input_lines[self._cursor_row]
            self._cursor_row -= 1
            self._cursor_col = new_cursor
            self._clear_attachment_suppression_on_edit()
        self._update_input_panels()

    def _delete_forward(self) -> None:
        if self._active_choice is not None:
            return
        self._reset_ctrl_c()
        line = self._current_line()
        col = min(self._cursor_col, len(line))
        if col < len(line):
            new_line = line[:col] + line[col + 1 :]
            self._set_current_line(new_line)
            self._clear_attachment_suppression_on_edit()
        elif self._cursor_row < len(self._input_lines) - 1:
            # Join with next line
            next_line = self._input_lines[self._cursor_row + 1]
            self._input_lines[self._cursor_row] = line + next_line
            del self._input_lines[self._cursor_row + 1]
            self._clear_attachment_suppression_on_edit()
        self._update_input_panels()

    def _insert_newline(self) -> None:
        if self._active_choice is not None:
            return
        self._reset_ctrl_c()
        line = self._current_line()
        col = min(self._cursor_col, len(line))
        before = line[:col]
        after = line[col:]
        self._input_lines[self._cursor_row] = before
        self._input_lines.insert(self._cursor_row + 1, after)
        self._cursor_row += 1
        self._cursor_col = 0
        self._clear_attachment_suppression_on_edit()
        self._update_input_panels()

    def _cursor_left(self) -> None:
        if self._active_choice is not None:
            return
        if self._cursor_col > 0:
            self._cursor_col -= 1
        elif self._cursor_row > 0:
            self._cursor_row -= 1
            self._cursor_col = len(self._current_line())
        self._update_input_panels()

    def _cursor_right(self) -> None:
        if self._active_choice is not None:
            return
        line = self._current_line()
        if self._cursor_col < len(line):
            self._cursor_col += 1
        elif self._cursor_row < len(self._input_lines) - 1:
            self._cursor_row += 1
            self._cursor_col = 0
        self._update_input_panels()

    def _cursor_home(self) -> None:
        if self._active_choice is not None:
            return
        self._cursor_col = 0
        self._update_input_panels()

    def _cursor_end(self) -> None:
        if self._active_choice is not None:
            return
        self._cursor_col = len(self._current_line())
        self._update_input_panels()

    def _clamp_cursor_col(self) -> None:
        self._cursor_col = min(self._cursor_col, len(self._current_line()))

    def _cursor_up_or_history(self) -> None:
        if self._cursor_row > 0:
            self._cursor_row -= 1
            self._clamp_cursor_col()
            self._update_input_panels()
            return
        self._history_prev()

    def _cursor_down_or_history(self) -> None:
        if self._cursor_row < len(self._input_lines) - 1:
            self._cursor_row += 1
            self._clamp_cursor_col()
            self._update_input_panels()
            return
        self._history_next()

    # ── history ──────────────────────────────────────────────────────────

    def _history_prev(self) -> None:
        if self._active_choice is not None or not self._input_history:
            return
        self._reset_ctrl_c()
        if self._history_idx == -1:
            self._history_draft = list(self._input_lines)
            self._history_draft_paste_entries = self._paste_entries_snapshot()
            self._history_idx = len(self._input_history) - 1
        elif self._history_idx > 0:
            self._history_idx -= 1
        self._load_history_item()

    def _history_next(self) -> None:
        if self._active_choice is not None:
            return
        if self._history_idx == -1:
            return
        self._reset_ctrl_c()
        self._history_idx += 1
        if self._history_idx >= len(self._input_history):
            self._history_idx = -1
            self._input_lines = list(self._history_draft)
            self._restore_paste_entries(self._history_draft_paste_entries)
            self._cursor_row = len(self._input_lines) - 1
            self._cursor_col = len(self._current_line())
            self._clear_attachment_suppression_on_edit()
            self._update_input_panels()
            return
        self._load_history_item()

    def _load_history_item(self) -> None:
        if 0 <= self._history_idx < len(self._input_history):
            text = self._input_history[self._history_idx]
            self._input_lines = text.split("\n")
            if 0 <= self._history_idx < len(self._input_history_paste_entries):
                self._restore_paste_entries(self._input_history_paste_entries[self._history_idx])
            else:
                self._clear_paste_entries()
            self._cursor_row = len(self._input_lines) - 1
            self._cursor_col = len(self._current_line())
            self._clear_attachment_suppression_on_edit()
            self._update_input_panels()

    def _record_history(self, text: str, paste_entries: list[dict[str, object]] | None = None) -> None:
        stripped = text.strip()
        if stripped and (not self._input_history or self._input_history[-1] != stripped):
            self._input_history.append(stripped)
            self._input_history_paste_entries.append([dict(entry) for entry in (paste_entries or [])])
            limit = max(int(self.INPUT_HISTORY_LIMIT), 0)
            if limit == 0:
                self._input_history.clear()
                self._input_history_paste_entries.clear()
            elif len(self._input_history) > limit:
                overflow = len(self._input_history) - limit
                del self._input_history[:overflow]
                del self._input_history_paste_entries[:overflow]
        self._history_idx = -1

    # ── input helpers ────────────────────────────────────────────────────

    def _is_input_empty(self) -> bool:
        return len(self._input_lines) == 1 and not self._input_lines[0]

    def _get_input_text(self) -> str:
        return "\n".join(self._input_lines)

    def _clear_input(self) -> None:
        self._input_lines = [""]
        self._cursor_row = 0
        self._cursor_col = 0
        self._command_panel_active = False
        self._attachment_selected = 0
        self._attachment_panel_suppressed_text = ""
        self._attachment_matches_cache_key = None
        self._attachment_matches_cache = []
        self._skill_selected = 0
        self._skill_panel_suppressed_text = ""
        self._skill_matches_cache_key = None
        self._skill_matches_cache = []
        self._clear_paste_entries()

    def _input_cursor_position(self) -> int:
        """Return the logical character offset used by attachment-token parsing."""
        cursor = 0
        for row in range(min(self._cursor_row, len(self._input_lines))):
            cursor += len(self._input_lines[row]) + 1
        if self._input_lines:
            cursor += min(self._cursor_col, len(self._current_line()))
        return cursor

    def _set_input_text_and_cursor(self, text: str, cursor: int) -> None:
        self._input_lines = text.split("\n") or [""]
        cursor = max(0, min(cursor, len(text)))
        before = text[:cursor]
        self._cursor_row = before.count("\n")
        last_newline = before.rfind("\n")
        self._cursor_col = len(before) if last_newline == -1 else len(before) - last_newline - 1
        self._clear_attachment_suppression_on_edit()

    def _insert_text_token(self, token: str) -> None:
        self._reset_ctrl_c()
        line = self._current_line()
        col = min(self._cursor_col, len(line))
        self._set_current_line(line[:col] + token + line[col:])
        self._cursor_col = col + len(token)
        self._clear_attachment_suppression_on_edit()
        self._update_input_panels()
