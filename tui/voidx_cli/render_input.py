"""Input region rendering helpers."""

from __future__ import annotations

from rich.cells import cell_len
from rich.text import Text


class _InputRendererMixin:
    def _render_line_width(self, width: int) -> int:
        return max(width, 1)

    def _input_line_prefix_width(self, row: int) -> int:
        if row == 0 and self._active_text_prompt is not None:
            return 0
        return 2

    def _input_display_text(self, line: str) -> str:
        if self._active_text_secret:
            return "*" * cell_len(line)
        return line

    def _input_display_rows(self, width: int) -> list[int]:
        render_width = self._render_line_width(width)
        rows: list[int] = []
        for row, line in enumerate(self._input_lines):
            cells = self._input_display_cell_count(row, line)
            rows.append(max((cells + render_width - 1) // render_width, 1))
        return rows or [1]

    def _input_display_cell_count(self, row: int, line: str) -> int:
        display = self._input_display_text(line)
        cells = self._input_line_prefix_width(row) + cell_len(display)
        if row == self._cursor_row and not self._active_choice:
            cursor = min(self._cursor_col, len(line))
            if self._active_text_secret:
                cursor_cells = cell_len(line[:cursor])
                display_cells = len(display)
            else:
                cursor_cells = cell_len(display[:cursor])
                display_cells = cell_len(display)
            if cursor_cells >= display_cells:
                cells += 1
        return cells

    def _render_input_line(
        self,
        row: int,
        line: str,
        prefix: str,
        width: int,
    ) -> list[Text]:
        segments: list[tuple[str, str]] = []
        if prefix:
            segments.append((prefix, "bold white"))

        display = self._input_display_text(line)
        line_segments = self._input_line_segments(display)
        if row == self._cursor_row and not self._active_choice:
            cursor = min(self._cursor_col, len(display))
            if self._active_text_secret:
                cursor = min(cell_len(line[: self._cursor_col]), len(display))
            segments.extend(self._input_line_segments_with_cursor(line_segments, cursor))
        else:
            segments.extend(line_segments)

        return self._wrap_input_segments(segments, width)

    def _input_line_segments(self, display: str) -> list[tuple[str, str]]:
        if self._active_text_secret:
            return [(display, "white")] if display else []
        tokens = self._registered_paste_displays()
        if not tokens:
            return [(display, "white")] if display else []

        segments: list[tuple[str, str]] = []
        pos = 0
        while pos < len(display):
            match_start = -1
            match_token = ""
            for token, _kind in tokens:
                index = display.find(token, pos)
                if index == -1:
                    continue
                if match_start == -1 or index < match_start:
                    match_start = index
                    match_token = token
            if match_start == -1:
                segments.append((display[pos:], "white"))
                break
            if match_start > pos:
                segments.append((display[pos:match_start], "white"))
            segments.append((match_token, "dim cyan"))
            pos = match_start + len(match_token)
        return segments

    def _input_line_segments_with_cursor(
        self,
        segments: list[tuple[str, str]],
        cursor: int,
    ) -> list[tuple[str, str]]:
        result: list[tuple[str, str]] = []
        seen = 0
        inserted = False
        for text, style in segments:
            if inserted or cursor >= seen + len(text):
                result.append((text, style))
                seen += len(text)
                continue
            local = max(cursor - seen, 0)
            before = text[:local]
            at = text[local : local + 1] or " "
            after = text[local + 1 :]
            if before:
                result.append((before, style))
            result.append((at, "reverse white"))
            if after:
                result.append((after, style))
            inserted = True
            seen += len(text)
        if not inserted:
            result.append((" ", "reverse white"))
        return result

    def _wrap_input_segments(
        self,
        segments: list[tuple[str, str]],
        width: int,
    ) -> list[Text]:
        rows: list[Text] = []
        current = Text()
        used = 0
        render_width = self._render_line_width(width)

        for text, style in segments:
            for char in text:
                char_width = cell_len(char)
                if char_width <= 0:
                    current.append(char, style=style)
                    continue
                if used > 0 and used + char_width > render_width:
                    rows.append(current)
                    current = Text()
                    used = 0
                current.append(char, style=style)
                used += char_width
                if used >= render_width:
                    rows.append(current)
                    current = Text()
                    used = 0

        if current.plain or not rows:
            rows.append(current)
        return rows
