"""Terminal frame rendering helpers."""

from __future__ import annotations

import io
import shutil
import sys
import time

from rich.cells import cell_len
from rich.console import Console, Group
from rich.text import Text

from voidx.presentation.output.dock import dock
from voidx.presentation.output.dock.formatting import text_from_line
from .helpers import _rendered_row_count
from .state import RenderStats


class _FrameRendererMixin:
    def _frame_width(self) -> int:
        return max((self._console.width or 80) - 1, 20)

    def _render_frame(self) -> None:
        """Render to terminal: capture Rich output, write with cursor control."""
        started_at = time.perf_counter()
        width = self._frame_width()
        term_height = shutil.get_terminal_size().lines if self._tty else None
        render_failed = False
        clear_screen = False
        if self._tty:
            term_height = term_height or shutil.get_terminal_size().lines
            force_full = render_failed or self._bottom_region_dirty
            if (
                self._prev_frame_width != 0
                and (
                    self._prev_frame_width != width
                    or self._prev_frame_term_height != term_height
                )
            ):
                self._invalidate_frame_cache()
                force_full = True
            if dock.consume_clear_screen_request():
                self._committed_line_count = 0
                self._visible_committed_rows = 0
                self._invalidate_frame_cache()
                force_full = True
                clear_screen = True
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
            if clear_screen:
                sys.stdout.write("\x1b[2J\x1b[H")
            frame_rows = _rendered_row_count(ansi)
            bottom_ansi = self._capture_renderable(self._render_bottom_impl(), width)
            bottom_rows = _rendered_row_count(bottom_ansi)
            busy_activity_rows = 0 if render_failed else self._busy_activity_row_count(width)
            scrolled = self._make_room_for_frame(frame_rows, term_height)
            if scrolled:
                self._invalidate_frame_cache()
                force_full = True
            start_row = max(self._visible_committed_rows + 1, 1)
            lines = ansi.splitlines()
            prev_lines = self._prev_frame_lines
            if (
                not force_full
                and prev_lines is not None
                and self._prev_frame_start_row == start_row
            ):
                changed_lines, strategy = self._render_diff(start_row, prev_lines, lines)
            else:
                changed_lines, strategy = self._render_full(start_row, lines)
            self._last_frame_rows = frame_rows
            self._last_frame_start_row = start_row
            self._last_bottom_rows = bottom_rows
            self._last_bottom_start_row = start_row + frame_rows - bottom_rows
            if busy_activity_rows > 0:
                thinking_stream_rows = len(self._active_thinking_stream_elements(width))
                self._record_busy_activity_layout(
                    start_row=(
                        start_row
                        + frame_rows
                        - bottom_rows
                        - thinking_stream_rows
                        - busy_activity_rows
                    ),
                    rows=busy_activity_rows,
                    width=width,
                    term_height=term_height,
                    bottom_rows=bottom_rows,
                    thinking_rows=thinking_stream_rows,
                )
            else:
                self._invalidate_busy_activity_layout()
            self._position_input_cursor(frame_rows)
            self._has_rendered_frame = True
            self._prev_frame_lines = lines
            self._prev_frame_start_row = start_row
            self._prev_frame_width = width
            self._prev_frame_term_height = term_height
            self._bottom_region_dirty = False
            self._render_stats = RenderStats(
                total_lines=len(lines),
                changed_lines=changed_lines,
                render_ms=(time.perf_counter() - started_at) * 1000,
                strategy=strategy,
            )
            sys.stdout.flush()
            if self._pending_tb:
                sys.stderr.write(self._pending_tb)
                sys.stderr.flush()
                self._pending_tb = ""
        else:
            return

    def _render_full(self, start_row: int, lines: list[str]) -> tuple[int, str]:
        sys.stdout.write(f"\x1b[{start_row};1H")
        sys.stdout.write("\x1b[J")
        sys.stdout.write("\n".join(lines))
        return len(lines), "full"

    def _render_diff(
        self,
        start_row: int,
        prev_lines: list[str],
        new_lines: list[str],
    ) -> tuple[int, str]:
        total = max(len(prev_lines), len(new_lines))
        changed = [
            index
            for index in range(total)
            if index >= len(prev_lines)
            or index >= len(new_lines)
            or prev_lines[index] != new_lines[index]
        ]
        if total and len(changed) / total > 0.8:
            return self._render_full(start_row, new_lines)

        wrote_tail_clear = False
        for index in changed:
            row = start_row + index
            sys.stdout.write(f"\x1b[{row};1H")
            if index >= len(new_lines):
                sys.stdout.write("\x1b[J")
                wrote_tail_clear = True
                break
            sys.stdout.write("\x1b[K")
            sys.stdout.write(new_lines[index])
        return len(changed), "diff-tail-clear" if wrote_tail_clear else "diff"

    def _invalidate_frame_cache(self) -> None:
        self._prev_frame_lines = None
        self._prev_frame_start_row = 1
        self._prev_frame_width = 0
        self._prev_frame_term_height = None
        self._invalidate_busy_activity_layout()

    def _record_busy_activity_layout(
        self,
        *,
        start_row: int,
        rows: int,
        width: int,
        term_height: int | None,
        bottom_rows: int,
        thinking_rows: int,
    ) -> None:
        self._last_busy_activity_start_row = start_row
        self._last_busy_activity_rows = rows
        self._last_busy_activity_width = width
        self._last_busy_activity_term_height = term_height
        self._last_busy_activity_bottom_rows = bottom_rows
        self._last_busy_activity_thinking_rows = thinking_rows

    def _invalidate_busy_activity_layout(self) -> None:
        self._last_busy_activity_rows = 0
        self._last_busy_activity_start_row = 0
        self._last_busy_activity_width = 0
        self._last_busy_activity_term_height = None
        self._last_busy_activity_bottom_rows = 0
        self._last_busy_activity_thinking_rows = 0

    def _busy_activity_layout_matches(self, *, width: int, term_height: int | None, rows: int) -> bool:
        return (
            self._last_busy_activity_start_row > 0
            and self._last_busy_activity_rows == rows
            and self._last_busy_activity_width == width
            and self._last_busy_activity_term_height == term_height
            and self._last_busy_activity_bottom_rows == self._last_bottom_rows
            and self._last_busy_activity_thinking_rows == len(self._active_thinking_stream_elements(width))
        )

    def _frame_geometry_changed(self) -> bool:
        if not self._tty or self._prev_frame_width == 0:
            return False
        return (
            self._prev_frame_width != self._frame_width()
            or self._prev_frame_term_height != shutil.get_terminal_size().lines
        )

    def _make_room_for_frame(self, frame_rows: int, term_height: int) -> bool:
        visible = max(0, min(self._visible_committed_rows, term_height))
        if visible != self._visible_committed_rows:
            self._visible_committed_rows = visible
        overlap = visible + frame_rows - term_height
        if overlap <= 0:
            return False
        scroll_rows = min(overlap, visible)
        if scroll_rows <= 0:
            self._visible_committed_rows = 0
            return False
        sys.stdout.write(f"\x1b[{term_height};1H")
        sys.stdout.write("\n" * scroll_rows)
        self._visible_committed_rows = visible - scroll_rows
        return True

    def _render_input_region(self) -> None:
        if not self._tty or not self._has_rendered_frame or self._last_bottom_rows <= 0:
            self._render_frame()
            return
        if self._frame_geometry_changed():
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
        self._bottom_region_dirty = True
        self._invalidate_busy_activity_layout()
        sys.stdout.flush()

    def _render_choice_selection_region(self) -> bool:
        if (
            not self._tty
            or self._active_choice is None
            or not self._has_rendered_frame
            or self._last_bottom_rows <= 0
        ):
            return False
        if self._frame_geometry_changed():
            self._render_frame()
            return True

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
            or not self._busy_activity_tick_active()
            or not self._has_rendered_frame
            or self._render_scheduled
        ):
            return False
        if self._frame_geometry_changed():
            return False

        width = self._frame_width()
        term_height = shutil.get_terminal_size().lines
        try:
            ansi = self._capture_renderable(
                Group(*self._render_busy_activity_elements(width)),
                width,
            )
        except Exception:
            return False

        rows = _rendered_row_count(ansi)
        if rows <= 0 or not self._busy_activity_layout_matches(
            width=width,
            term_height=term_height,
            rows=rows,
        ):
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
        # Strip only the newline added by Console.print. Meaningful trailing
        # blank rows are part of the transcript and must still count.
        return ansi[:-1] if ansi.endswith("\n") else ansi


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
        panel_rows = self._visible_panel_row_count(width)
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
            + panel_rows
            + (1 if panel_rows else 0)
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
        thinking_stream_elements = self._active_thinking_stream_elements(width)

        base_bottom_rows = self._base_bottom_row_count(width, status_lines)
        panel_rows, panel_ansi = self._panel_row_count_and_ansi(panel_lines, width)
        if panel_lines:
            panel_row_limit = max(
                render_height
                - base_bottom_rows
                - len(busy_activity_elements)
                - len(thinking_stream_elements)
                - 1,
                0,
            )
            self._panel_row_limit = panel_row_limit
            panel_rows = min(panel_rows, panel_row_limit)
        else:
            self._panel_row_limit = None
        bottom_fixed_lines = (
            base_bottom_rows
            + panel_rows
            + (1 if panel_rows else 0)
        )
        todo_budget = max(
            render_height
            - bottom_fixed_lines
            - len(busy_activity_elements)
            - len(thinking_stream_elements),
            0,
        )
        todo_max_rows = min(
            self._pinned_todo_max_rows(
                render_height,
                bottom_fixed_lines
                + len(busy_activity_elements)
                + len(thinking_stream_elements),
            ),
            todo_budget,
        )
        pinned_todo_elements = self._render_pinned_todo_elements(width, max_rows=todo_max_rows)
        fixed_lines = (
            bottom_fixed_lines
            + len(pinned_todo_elements)
            + len(busy_activity_elements)
            + len(thinking_stream_elements)
        )
        body_limit = max(render_height - fixed_lines, 0)

        # Transcript — only render uncommitted (active) lines.
        # Restored history stays in the viewport; only new, uncommitted root
        # blocks participate in the active frame after the restore boundary.
        committed = self._committed_line_count
        restored_range = self._sync_restored_render_state()
        if restored_range is not None:
            restored_start, restored_end = restored_range
            history_start = restored_start if self._restored_startup_flushed else 0
            current_end = len(dock.tree.root.children)
            tail_limit = max(body_limit * 2, body_limit + 32)
            history_lines, history_line_map = dock.tree.render_root_tail_with_line_map(
                width,
                history_start,
                restored_end,
                tail_limit,
            )
            added_lines, added_line_map = dock.tree.render_root_slice_with_line_map(
                width,
                restored_end,
                current_end,
            )
            committed_added = min(
                self._restored_committed_line_count,
                len(added_lines),
            )
            thinking_node_id = dock.active_thinking_stream_node_id()
            active_lines = [
                line
                for index, line in enumerate(history_lines)
                if history_line_map.get(index) != thinking_node_id
            ]
            active_lines.extend(
                line
                for index, line in enumerate(added_lines[committed_added:], start=committed_added)
                if added_line_map.get(index) != thinking_node_id
            )
        else:
            use_tail = committed == 0 and dock.tree.node_count >= 256
            if use_tail:
                tail_limit = max(body_limit * 2, body_limit + 32)
                tree_lines, tail_line_map = dock.tree.render_tail_with_line_map(
                    width,
                    tail_limit,
                )
                thinking_node_id = dock.active_thinking_stream_node_id()
                active_lines = [
                    line
                    for index, line in enumerate(tree_lines)
                    if tail_line_map.get(index) != thinking_node_id
                ]
            else:
                tree_lines = dock.tree.render(width)
                thinking_line_ids = dock.active_thinking_stream_line_ids(width)
                active_lines = [
                    line
                    for index, line in enumerate(tree_lines[committed:], start=committed)
                    if index not in thinking_line_ids
                ]

        elements: list = self._transcript_elements_for_rows(
            active_lines,
            width,
            body_limit,
        )

        elements.extend(pinned_todo_elements)
        elements.extend(busy_activity_elements)
        elements.extend(thinking_stream_elements)
        elements.extend(
            self._render_bottom_elements(
                width,
                panel_lines,
                status_lines,
                panel_ansi=panel_ansi,
            )
        )

        return Group(*elements)

    def _active_thinking_stream_elements(self, width: int) -> list[Text]:
        lines = dock.active_thinking_stream_lines(width)
        if not lines:
            return []
        return self._transcript_elements_for_rows(lines, width, len(lines))

    def _transcript_elements_for_rows(
        self,
        lines: list[str],
        width: int,
        row_limit: int,
    ) -> list[Text]:
        if not lines or row_limit <= 0:
            return []

        target_rows = row_limit + min(max(row_limit, 8), 32)
        renderables: list[Text] = []
        visual_rows = 0
        for line in reversed(lines):
            try:
                rendered = text_from_line(line)
            except Exception:
                rendered = Text(line)
            renderables.insert(0, rendered)
            visual_rows = _rendered_row_count(
                self._capture_renderable(Group(*renderables), width)
            )
            if visual_rows >= target_rows:
                break

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
        *,
        panel_ansi: str | None = None,
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
        panel_elements = self._render_panel_elements(
            panel_lines,
            width,
            panel_ansi=panel_ansi,
        )
        elements.extend(panel_elements)

        if panel_elements:
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

    def _base_bottom_row_count(self, width: int, status_lines: list) -> int:
        elements = self._render_bottom_elements(width, [], status_lines)
        key = (
            width,
            self._console.height,
            tuple(self._renderable_cache_key(element) for element in elements),
        )
        if key == self._base_bottom_rows_cache_key:
            return self._base_bottom_rows_cache_count
        count = _rendered_row_count(self._capture_renderable(Group(*elements), width))
        self._base_bottom_rows_cache_key = key
        self._base_bottom_rows_cache_count = count
        return count

    @staticmethod
    def _renderable_cache_key(renderable: object) -> tuple:
        if isinstance(renderable, Text):
            return (
                "text",
                renderable.plain,
                str(renderable.style),
                tuple(
                    (span.start, span.end, str(span.style))
                    for span in renderable.spans
                ),
            )
        return ("repr", repr(renderable))

    def _render_panel_elements(
        self,
        panel_lines: list[str],
        width: int,
        *,
        panel_ansi: str | None = None,
    ) -> list[Text]:
        if not panel_lines:
            return []
        elements = [Text.from_markup(line) for line in panel_lines]
        row_limit = self._panel_row_limit
        if row_limit is None:
            return elements
        if row_limit <= 0:
            return []
        ansi = panel_ansi
        if ansi is None:
            ansi = self._capture_renderable(Group(*elements), width)
        rows = ansi.splitlines()
        if len(rows) <= row_limit:
            return elements
        if row_limit == 1:
            return [Text("…", style="dim")]
        visible_rows = rows[-(row_limit - 1):]
        return [Text("…", style="dim")] + [Text.from_ansi(row) for row in visible_rows]

    def _panel_row_count_and_ansi(
        self,
        panel_lines: list[str],
        width: int,
    ) -> tuple[int, str | None]:
        if not panel_lines:
            return 0, None
        elements = [Text.from_markup(line) for line in panel_lines]
        ansi = self._capture_renderable(Group(*elements), width)
        return _rendered_row_count(ansi), ansi

    def _panel_row_count(self, panel_lines: list[str], width: int) -> int:
        return self._panel_row_count_and_ansi(panel_lines, width)[0]

    def _visible_panel_row_count(
        self,
        width: int,
        *,
        panel_lines: list[str] | None = None,
    ) -> int:
        lines = self._render_panel_lines(width) if panel_lines is None else panel_lines
        rows = self._panel_row_count(lines, width)
        row_limit = self._panel_row_limit
        if row_limit is None:
            return rows
        return min(rows, max(row_limit, 0))
