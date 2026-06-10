"""Terminal frame rendering helpers."""

from __future__ import annotations

import io
import shutil
import sys

from rich.cells import cell_len
from rich.console import Console, Group
from rich.text import Text

from voidx.ui.output.dock import dock
from voidx.ui.output.dock.formatting import _text_from_line
from voidx.ui.tui.helpers import _rendered_row_count


class _FrameRendererMixin:
    def _frame_width(self) -> int:
        return max((self._console.width or 80) - 1, 20)

    def _render_frame(self) -> None:
        """Render to terminal: capture Rich output, write with cursor control."""
        width = self._frame_width()
        term_height = shutil.get_terminal_size().lines if self._tty else None
        render_failed = False
        try:
            renderable = self._render_impl(height=term_height)
        except Exception as exc:
            import traceback

            render_failed = True
            self._pending_tb = traceback.format_exc()
            self._last_error = f"Render error: {exc}"
            renderable = Group(Text(f"Render error: {exc}", style="red"))

        ansi = self._capture_renderable(renderable, width)

        if self._tty:
            term_height = term_height or shutil.get_terminal_size().lines
            if dock.consume_clear_screen_request():
                self._committed_line_count = 0
                self._visible_committed_rows = 0
                sys.stdout.write("\x1b[2J\x1b[H")
            frame_rows = _rendered_row_count(ansi)
            bottom_ansi = self._capture_renderable(self._render_bottom_impl(), width)
            bottom_rows = _rendered_row_count(bottom_ansi)
            busy_activity_rows = 0 if render_failed else self._busy_activity_row_count(width)
            self._make_room_for_frame(frame_rows, term_height)
            start_row = max(self._visible_committed_rows + 1, 1)
            sys.stdout.write(f"\x1b[{start_row};1H")
            sys.stdout.write("\x1b[J")
            sys.stdout.write(ansi)
            self._last_frame_rows = frame_rows
            self._last_frame_start_row = start_row
            self._last_bottom_rows = bottom_rows
            self._last_bottom_start_row = start_row + frame_rows - bottom_rows
            self._last_busy_activity_rows = busy_activity_rows
            if busy_activity_rows > 0:
                self._last_busy_activity_start_row = (
                    start_row + frame_rows - bottom_rows - busy_activity_rows
                )
            else:
                self._last_busy_activity_start_row = 0
            self._position_input_cursor(frame_rows)
            self._has_rendered_frame = True
            sys.stdout.flush()
            if self._pending_tb:
                sys.stderr.write(self._pending_tb)
                sys.stderr.flush()
                self._pending_tb = ""
        else:
            return

    def _make_room_for_frame(self, frame_rows: int, term_height: int) -> None:
        visible = max(0, min(self._visible_committed_rows, term_height))
        if visible != self._visible_committed_rows:
            self._visible_committed_rows = visible
        overlap = visible + frame_rows - term_height
        if overlap <= 0:
            return
        scroll_rows = min(overlap, visible)
        if scroll_rows <= 0:
            self._visible_committed_rows = 0
            return
        sys.stdout.write(f"\x1b[{term_height};1H")
        sys.stdout.write("\n" * scroll_rows)
        self._visible_committed_rows = visible - scroll_rows

    def _render_input_region(self) -> None:
        if not self._tty or not self._has_rendered_frame or self._last_bottom_rows <= 0:
            self._render_frame()
            return

        width = self._frame_width()
        try:
            ansi = self._capture_renderable(self._render_bottom_impl(), width)
        except Exception:
            self._render_frame()
            return

        bottom_rows = _rendered_row_count(ansi)
        # Use the stored bottom start row (computed from frame start + offset)
        # rather than re-deriving from terminal height, so it works correctly
        # when the frame is top-aligned.
        start_row = self._last_bottom_start_row
        if (
            bottom_rows != self._last_bottom_rows
            or start_row != self._last_bottom_start_row
        ):
            self._render_frame()
            return

        sys.stdout.write(f"\x1b[{start_row};1H")
        sys.stdout.write("\x1b[J")
        sys.stdout.write(ansi)
        self._position_input_cursor(self._last_frame_rows)
        self._has_rendered_frame = True
        sys.stdout.flush()

    def _render_choice_selection_region(self) -> bool:
        if (
            not self._tty
            or self._active_choice is None
            or not self._has_rendered_frame
            or self._last_bottom_rows <= 0
        ):
            return False

        width = self._frame_width()
        try:
            ansi = self._capture_renderable(self._render_bottom_impl(), width)
        except Exception:
            return False

        bottom_rows = _rendered_row_count(ansi)
        if bottom_rows != self._last_bottom_rows:
            return False

        start_row = self._last_bottom_start_row
        if start_row <= 0:
            return False

        lines = ansi.splitlines()
        if len(lines) != bottom_rows:
            return False

        for offset, line in enumerate(lines):
            sys.stdout.write(f"\x1b[{start_row + offset};1H")
            sys.stdout.write(line)
            sys.stdout.write("\x1b[K")
        self._position_input_cursor(self._last_frame_rows)
        self._has_rendered_frame = True
        sys.stdout.flush()
        return True

    def _render_busy_activity_tick(self) -> bool:
        if (
            not self._tty
            or not self._busy
            or not self._has_rendered_frame
            or self._last_busy_activity_rows <= 0
            or self._last_busy_activity_start_row <= 0
        ):
            return False

        width = self._frame_width()
        try:
            ansi = self._capture_renderable(
                Group(*self._render_busy_activity_elements(width)),
                width,
            )
        except Exception:
            return False

        rows = _rendered_row_count(ansi)
        if rows != self._last_busy_activity_rows:
            return False

        lines = ansi.splitlines()
        if len(lines) != rows:
            return False

        start_row = self._last_busy_activity_start_row
        for offset, line in enumerate(lines):
            sys.stdout.write(f"\x1b[{start_row + offset};1H")
            sys.stdout.write(line)
            sys.stdout.write("\x1b[K")
        frame_end_row = self._last_frame_start_row + self._last_frame_rows - 1
        sys.stdout.write(f"\x1b[{max(frame_end_row, 1)};1H")
        self._position_input_cursor(self._last_frame_rows)
        sys.stdout.flush()
        return True

    def _capture_renderable(self, renderable: object, width: int) -> str:
        capture_width = max(width, 1)
        key = (capture_width, self._console.height)
        if self._capture_console is None or self._capture_console_key != key:
            self._capture_buffer = io.StringIO()
            self._capture_console = Console(
                file=self._capture_buffer,
                force_terminal=True,
                color_system="truecolor",
                width=capture_width,
                height=self._console.height,
            )
            self._capture_console_key = key
        else:
            # Reuse the capture console but clear the backing buffer first.
            self._capture_buffer.seek(0)
            self._capture_buffer.truncate(0)
        self._capture_console.print(renderable)
        ansi = self._capture_buffer.getvalue()
        # Strip the trailing newline added by Console.print so that writing
        # the captured output to the terminal never advances the cursor past
        # the last rendered line (which would trigger an unwanted scroll).
        return ansi.rstrip("\n")

    def _move_to_frame_top_sequence(self) -> str:
        if not self._has_rendered_frame:
            return ""
        if self._last_frame_rows <= 0:
            return ""
        return f"\x1b[{self._last_frame_rows}A"

    def _move_to_frame_end_sequence(self) -> str:
        if not self._has_rendered_frame:
            return ""
        if self._cursor_to_frame_end_lines <= 0:
            return "\r"
        return f"\r\x1b[{self._cursor_to_frame_end_lines}B\r"

    def _position_input_cursor(self, frame_rows: int | None = None) -> None:
        """Move terminal cursor to the current input cursor position."""
        width = self._frame_width()
        status_lines = self._render_hint_lines()
        panel_lines = self._render_panel_lines(width)
        input_rows = self._input_display_rows(width)
        cursor_row = min(self._cursor_row, max(len(input_rows) - 1, 0))
        current_line = self._current_line()
        display_line = self._input_display_text(current_line)
        cursor = min(self._cursor_col, len(current_line))
        render_width = self._render_line_width(width)
        if self._active_text_secret:
            before_cursor = "*" * cell_len(current_line[:cursor])
        else:
            before_cursor = display_line[:cursor]
        cursor_cells = self._input_line_prefix_width(cursor_row) + cell_len(before_cursor)
        cursor_visual_row = min(cursor_cells // render_width, input_rows[cursor_row] - 1)
        # Count only rows below the cursor; the native cursor anchors IME popups.
        rows_after_cursor = (
            input_rows[cursor_row]
            - cursor_visual_row
            - 1
            + sum(input_rows[cursor_row + 1 :])
        )
        lines_up = (
            rows_after_cursor
            + 1  # input bottom border
            + len(panel_lines)
            + (1 if panel_lines else 0)
            + len(status_lines)
        )
        col = cursor_cells % render_width
        sys.stdout.write(f"\x1b[{lines_up}A\x1b[{col + 1}G")
        if frame_rows is not None:
            self._cursor_to_frame_top_lines = max(frame_rows - lines_up, 0)
            self._cursor_to_frame_end_lines = lines_up
            self._last_frame_rows = frame_rows

    def _render_impl(self, *, height: int | None = None) -> Group:
        width = self._frame_width()
        render_height = max(height or self._console.height or 24, 1)

        # Cross-mixin render hooks: status, input panel, busy activity, and pinned todo.
        status_lines = self._render_hint_lines()
        panel_lines = self._render_panel_lines(width)
        busy_activity_elements = self._render_busy_activity_elements(width)

        bottom_fixed_lines = (
            1  # transcript/input separator
            + (1 if self._active_text_prompt is not None else 0)
            + sum(self._input_display_rows(width))
            + 1  # input bottom border
            + len(panel_lines)
            + (1 if panel_lines else 0)
            + len(status_lines)
        )
        todo_max_rows = self._pinned_todo_max_rows(
            render_height,
            bottom_fixed_lines + len(busy_activity_elements),
        )
        pinned_todo_elements = self._render_pinned_todo_elements(width, max_rows=todo_max_rows)
        fixed_lines = (
            bottom_fixed_lines
            + len(pinned_todo_elements)
            + len(busy_activity_elements)
        )
        body_limit = max(render_height - fixed_lines, 1)

        # Transcript — only render uncommitted (active) lines.
        # Committed lines have already been flushed to terminal scrollback.
        tree_lines = dock.tree.render(width)
        committed = self._committed_line_count
        active_lines = tree_lines[committed:]

        elements: list = self._transcript_elements_for_rows(
            active_lines,
            width,
            body_limit,
        )

        elements.extend(pinned_todo_elements)
        elements.extend(busy_activity_elements)
        elements.extend(
            self._render_bottom_elements(width, panel_lines, status_lines)
        )

        return Group(*elements)

    def _transcript_elements_for_rows(
        self,
        lines: list[str],
        width: int,
        row_limit: int,
    ) -> list[Text]:
        if not lines or row_limit <= 0:
            return []

        renderables: list[Text] = []
        for line in lines:
            try:
                renderables.append(_text_from_line(line))
            except Exception:
                renderables.append(Text(line))

        ansi = self._capture_renderable(Group(*renderables), width)
        if not ansi:
            return []

        tail_rows = ansi.splitlines()[-row_limit:]
        return [Text.from_ansi(row) for row in tail_rows]

    def _render_bottom_impl(self) -> Group:
        width = self._frame_width()
        return Group(
            *self._render_bottom_elements(
                width,
                self._render_panel_lines(width),
                self._render_hint_lines(),
            )
        )

    def _render_bottom_elements(
        self,
        width: int,
        panel_lines: list[str],
        status_lines: list,
    ) -> list:
        elements: list = []

        elements.append(Text("─" * width, style="dim"))

        # Input box
        input_border = "─" * width
        prompt = "❯ "

        if self._active_text_prompt is not None:
            elements.append(Text(f"{self._active_text_prompt} ", style="bold"))
            prompt = ""

        prompt_width = 2
        for row, line in enumerate(self._input_lines):
            prefix = prompt if row == 0 else " " * prompt_width
            elements.extend(self._render_input_line(row, line, prefix, width))

        elements.append(Text(input_border, style="dim"))

        # Panels (attachment, command palette, choice)
        for line in panel_lines:
            elements.append(Text.from_markup(line))

        if panel_lines:
            elements.append(Text("─" * width, style="dim"))

        # Status bar (always at the very bottom)
        for line in status_lines:
            elements.append(line)

        return elements

    def _render_hint_lines(self) -> list:
        lines: list = []
        status = self._status_summary_text(self._frame_width())
        if status.plain:
            lines.append(status)
        if self._notice:
            lines.append(Text("  " + self._notice, style="#8F9BA8"))
        if self._last_error:
            lines.append(Text("  ⚠ " + self._last_error, style="red"))
        return lines
