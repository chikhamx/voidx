"""Terminal frame rendering helpers."""

from __future__ import annotations

import io
import shutil
import sys
import time
from dataclasses import dataclass

from rich.cells import cell_len
from rich.console import Console, Group
from rich.text import Text

from voidx.presentation.output.dock import dock
from voidx.presentation.output.dock.formatting import text_from_line
from .helpers import _rendered_row_count
from .state import RenderStats
from .terminal_writer import FrameBatch, FrameResult


@dataclass(frozen=True)
class _RenderPlan:
    width: int
    height: int
    status_lines: tuple[object, ...]
    panel_lines: tuple[str, ...]
    busy_activity_elements: tuple[Text, ...]
    thinking_stream_elements: tuple[Text, ...]
    base_bottom_rows: int
    panel_rows: int
    panel_ansi: str | None
    bottom_elements: tuple[object, ...]
    input_rows: tuple[int, ...]






class _FrameRendererMixin:
    def _frame_width(self) -> int:
        return max((self._console.width or 80) - 1, 20)

    def _terminal_writer_worker_mode(self) -> bool:
        return bool(getattr(self._terminal_writer, "worker_mode", False))

    def _handle_terminal_frame_result(self, result: FrameResult) -> None:
        if not result.applied or result.generation != self._terminal_frame_generation:
            return
        self._render_stats = RenderStats(
            total_lines=result.total_lines,
            changed_lines=result.changed_lines,
            render_ms=result.render_ms,
            strategy=result.strategy,
        )

    def _render_frame(self) -> None:
        """Render to terminal: capture Rich output, write with cursor control."""
        started_at = time.perf_counter()
        width = self._frame_width()
        term_height = shutil.get_terminal_size().lines if self._tty else None
        render_failed = False
        worker_mode = self._tty and self._terminal_writer_worker_mode()
        resize_frame = False
        clear_screen = False
        clear_submitted = False
        force_full = False
        if self._tty:
            term_height = term_height or shutil.get_terminal_size().lines
            resize_frame = self._prev_frame_width != 0 and (
                self._prev_frame_width != width
                or self._prev_frame_term_height != term_height
            )
            clear_screen = dock.consume_clear_screen_request()
            force_full = self._bottom_region_dirty or resize_frame or clear_screen
            if not worker_mode:
                if resize_frame:
                    self._invalidate_frame_cache()
                if clear_screen:
                    self._committed_line_count = 0
                    self._visible_committed_rows = 0
                    self._invalidate_frame_cache()

        self._render_plan = None
        try:
            try:
                renderable = self._render_impl(height=term_height, capture_plan=True)
                render_plan = self._render_plan
            except Exception as exc:
                import traceback

                render_plan = None
                render_failed = True
                if self._tty:
                    force_full = True
                self._pending_tb = traceback.format_exc()
                self._last_error = f"Render error: {exc}"
                renderable = Group(Text(f"Render error: {exc}", style="red"))

            ansi = self._capture_renderable(renderable, width)
            if not self._tty:
                return

            frame_rows = _rendered_row_count(ansi)
            if render_plan is None:
                bottom_renderable = self._render_bottom_impl()
            else:
                bottom_renderable = Group(*render_plan.bottom_elements)
            bottom_ansi = self._capture_renderable(bottom_renderable, width)
            bottom_rows = _rendered_row_count(bottom_ansi)
            busy_activity_rows = (
                0
                if render_failed
                else (
                    len(render_plan.busy_activity_elements)
                    if render_plan is not None
                    else self._busy_activity_row_count(width)
                )
            )
            thinking_stream_rows = (
                len(render_plan.thinking_stream_elements)
                if render_plan is not None
                else len(self._active_thinking_stream_elements(width))
            )
            lines = ansi.splitlines()

            if worker_mode:
                visible_before = 0 if clear_screen else self._visible_committed_rows
                visible_after, scroll_ansi = self._frame_scroll_plan(
                    frame_rows,
                    term_height,
                    visible_rows=visible_before,
                )
                force_full = force_full or bool(scroll_ansi)
                start_row = max(visible_after + 1, 1)
                cursor_ansi, lines_up = self._input_cursor_target(plan=render_plan)
                generation = self._terminal_frame_generation + 1
                render_ms = (time.perf_counter() - started_at) * 1000
                batch = FrameBatch(
                    generation=generation,
                    start_row=start_row,
                    target_lines=tuple(lines),
                    cursor_ansi=cursor_ansi,
                    render_ms=render_ms,
                    force_full=force_full,
                )

                if resize_frame:
                    self._terminal_writer.submit_barrier(kind="resize")
                    self._invalidate_frame_cache()
                if clear_screen:
                    self._terminal_writer.submit_barrier(
                        kind="clear",
                        ansi="\x1b[2J\x1b[H",
                    )
                    clear_submitted = True
                    self._committed_line_count = 0
                    self._visible_committed_rows = 0
                    self._invalidate_frame_cache()
                if scroll_ansi:
                    self._terminal_writer.submit_barrier(
                        kind="scroll",
                        ansi=scroll_ansi,
                    )
                    self._visible_committed_rows = visible_after
                    self._invalidate_frame_cache()

                self._terminal_writer.submit_frame(batch)
                self._terminal_frame_generation = generation
                self._visible_committed_rows = visible_after
                self._last_frame_rows = frame_rows
                self._last_frame_start_row = start_row
                self._last_bottom_rows = bottom_rows
                self._last_bottom_start_row = start_row + frame_rows - bottom_rows
                if busy_activity_rows > 0:
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
                self._record_input_cursor_geometry(frame_rows, lines_up)
                self._has_rendered_frame = True
                self._prev_frame_lines = lines
                self._prev_frame_start_row = start_row
                self._prev_frame_width = width
                self._prev_frame_term_height = term_height
                self._bottom_region_dirty = False
                self._last_render_plan = render_plan
            else:
                if clear_screen:
                    self._terminal_writer.write("\x1b[2J\x1b[H")
                scrolled = self._make_room_for_frame(frame_rows, term_height)
                if scrolled:
                    self._invalidate_frame_cache()
                    force_full = True
                start_row = max(self._visible_committed_rows + 1, 1)
                prev_lines = self._prev_frame_lines
                if (
                    not force_full
                    and prev_lines is not None
                    and self._prev_frame_start_row == start_row
                ):
                    changed_lines, strategy = self._render_diff(
                        start_row,
                        prev_lines,
                        lines,
                    )
                else:
                    changed_lines, strategy = self._render_full(start_row, lines)
                self._last_frame_rows = frame_rows
                self._last_frame_start_row = start_row
                self._last_bottom_rows = bottom_rows
                self._last_bottom_start_row = start_row + frame_rows - bottom_rows
                if busy_activity_rows > 0:
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
                self._position_input_cursor(frame_rows, plan=render_plan)
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
                self._terminal_writer.flush()
                self._last_render_plan = render_plan

            if self._pending_tb:
                sys.stderr.write(self._pending_tb)
                sys.stderr.flush()
                self._pending_tb = ""
        finally:
            self._render_plan = None
            if worker_mode and clear_screen and not clear_submitted:
                dock.request_clear_screen()

    def _render_full(self, start_row: int, lines: list[str]) -> tuple[int, str]:
        self._terminal_writer.write(f"\x1b[{start_row};1H")
        self._terminal_writer.write("\x1b[J")
        self._terminal_writer.write("\n".join(lines))
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
            self._terminal_writer.write(f"\x1b[{row};1H")
            if index >= len(new_lines):
                self._terminal_writer.write("\x1b[J")
                wrote_tail_clear = True
                break
            self._terminal_writer.write("\x1b[K")
            self._terminal_writer.write(new_lines[index])
        return len(changed), "diff-tail-clear" if wrote_tail_clear else "diff"

    def _invalidate_frame_cache(self) -> None:
        self._last_render_plan = None
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

    def _busy_activity_layout_matches(
        self,
        *,
        plan: _RenderPlan,
        width: int,
        term_height: int | None,
        rows: int,
    ) -> bool:
        expected_height = max(term_height or self._console.height or 24, 1)
        return (
            self._last_busy_activity_start_row > 0
            and self._last_busy_activity_rows == rows
            and plan.width == width
            and plan.height == expected_height
            and self._last_busy_activity_width == width
            and self._last_busy_activity_term_height == term_height
            and self._last_busy_activity_bottom_rows == self._last_bottom_rows
            and self._last_busy_activity_thinking_rows == len(plan.thinking_stream_elements)
        )

    def _frame_geometry_changed(self) -> bool:
        if not self._tty or self._prev_frame_width == 0:
            return False
        return (
            self._prev_frame_width != self._frame_width()
            or self._prev_frame_term_height != shutil.get_terminal_size().lines
        )

    def _frame_scroll_plan(
        self,
        frame_rows: int,
        term_height: int,
        *,
        visible_rows: int | None = None,
    ) -> tuple[int, str]:
        visible = self._visible_committed_rows if visible_rows is None else visible_rows
        visible = max(0, min(visible, term_height))
        overlap = visible + frame_rows - term_height
        if overlap <= 0:
            return visible, ""
        scroll_rows = min(overlap, visible)
        if scroll_rows <= 0:
            return 0, ""
        return (
            visible - scroll_rows,
            f"\x1b[{term_height};1H" + "\n" * scroll_rows,
        )

    def _make_room_for_frame(self, frame_rows: int, term_height: int) -> bool:
        visible_after, scroll_ansi = self._frame_scroll_plan(frame_rows, term_height)
        if scroll_ansi:
            if self._terminal_writer_worker_mode():
                self._terminal_writer.submit_barrier(
                    kind="scroll",
                    ansi=scroll_ansi,
                )
            else:
                self._terminal_writer.write(scroll_ansi)
        self._visible_committed_rows = visible_after
        return bool(scroll_ansi)

    def _render_input_region(self) -> None:
        if self._tty and self._terminal_writer_worker_mode():
            self._render_frame()
            return
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

        self._terminal_writer.write(f"\x1b[{start_row};1H")
        self._terminal_writer.write("\x1b[J")
        self._terminal_writer.write(ansi)
        self._position_input_cursor(self._last_frame_rows)
        self._has_rendered_frame = True
        self._bottom_region_dirty = True
        self._last_render_plan = None
        self._invalidate_busy_activity_layout()
        self._terminal_writer.flush()

    def _render_choice_selection_region(self) -> bool:
        if not self._tty or self._active_choice is None:
            return False
        if self._terminal_writer_worker_mode():
            self._render_frame()
            return True
        if not self._has_rendered_frame or self._last_bottom_rows <= 0:
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
            self._terminal_writer.write(f"\x1b[{start_row + offset};1H")
            self._terminal_writer.write(line)
            self._terminal_writer.write("\x1b[K")
        self._position_input_cursor(self._last_frame_rows)
        self._has_rendered_frame = True
        self._last_render_plan = None
        self._terminal_writer.flush()
        return True

    def _render_busy_activity_tick(self) -> bool:
        if (
            not self._tty
            or not self._busy_activity_tick_active()
            or not self._has_rendered_frame
            or self._render_scheduled
        ):
            return False
        if self._terminal_writer_worker_mode():
            self._render_frame()
            return True
        if self._frame_geometry_changed():
            return False

        plan = self._last_render_plan
        if plan is None:
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
            plan=plan,
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
            self._terminal_writer.write(f"\x1b[{start_row + offset};1H")
            self._terminal_writer.write(line)
            self._terminal_writer.write("\x1b[K")
        frame_end_row = self._last_frame_start_row + self._last_frame_rows - 1
        self._terminal_writer.write(f"\x1b[{max(frame_end_row, 1)};1H")
        self._position_input_cursor(self._last_frame_rows, plan=plan)
        self._terminal_writer.flush()
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

    def _input_cursor_target(
        self,
        *,
        plan: _RenderPlan | None = None,
    ) -> tuple[str, int]:
        if plan is None:
            width = self._frame_width()
            status_lines = self._render_hint_lines()
            panel_rows = self._visible_panel_row_count(width)
            input_rows = self._input_display_rows(width)
        else:
            width = plan.width
            status_lines = plan.status_lines
            panel_rows = plan.panel_rows
            input_rows = plan.input_rows
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
        rows_after_cursor = (
            input_rows[cursor_row]
            - cursor_visual_row
            - 1
            + sum(input_rows[cursor_row + 1 :])
        )
        lines_up = (
            rows_after_cursor
            + 1
            + panel_rows
            + (1 if panel_rows else 0)
            + len(status_lines)
        )
        col = cursor_cells % render_width
        return f"\x1b[{lines_up}A\x1b[{col + 1}G", lines_up

    def _input_cursor_sequence(
        self,
        *,
        plan: _RenderPlan | None = None,
    ) -> str:
        sequence, _ = self._input_cursor_target(plan=plan)
        return sequence

    def _record_input_cursor_geometry(self, frame_rows: int, lines_up: int) -> None:
        self._cursor_to_frame_top_lines = max(frame_rows - lines_up, 0)
        self._cursor_to_frame_end_lines = lines_up
        self._last_frame_rows = frame_rows

    def _position_input_cursor(
        self,
        frame_rows: int | None = None,
        *,
        plan: _RenderPlan | None = None,
    ) -> None:
        """Move terminal cursor to the current input cursor position."""
        sequence, lines_up = self._input_cursor_target(plan=plan)
        self._terminal_writer.write(sequence)
        self._terminal_writer.flush()
        if frame_rows is not None:
            self._record_input_cursor_geometry(frame_rows, lines_up)

    def _render_impl(
        self,
        *,
        height: int | None = None,
        capture_plan: bool = False,
    ) -> Group:
        width = self._frame_width()
        render_height = max(height or self._console.height or 24, 1)

        # Cross-mixin render hooks: status, panel, busy activity, thinking, and input.
        status_lines = self._render_hint_lines()
        panel_lines = self._render_panel_lines(width)
        busy_activity_elements = self._render_busy_activity_elements(width)
        thinking_stream_elements = self._active_thinking_stream_elements(width)
        input_rows = self._input_display_rows(width)
        input_elements = self._render_input_elements(width)

        base_bottom_rows = self._base_bottom_row_count(
            width,
            status_lines,
            input_elements=input_elements,
        )
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
            panel_rows, panel_ansi = self._panel_row_count_and_ansi(panel_lines, width)
        else:
            self._panel_row_limit = None
            panel_rows, panel_ansi = 0, None
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
            if self._restored_history_retired:
                history_lines, history_line_map = [], {}
            else:
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
        bottom_elements = self._render_bottom_elements(
            width,
            panel_lines,
            status_lines,
            panel_ansi=panel_ansi,
            input_elements=input_elements,
        )
        elements.extend(bottom_elements)

        if capture_plan:
            self._render_plan = _RenderPlan(
                width=width,
                height=render_height,
                status_lines=tuple(status_lines),
                panel_lines=tuple(panel_lines),
                busy_activity_elements=tuple(busy_activity_elements),
                thinking_stream_elements=tuple(thinking_stream_elements),
                base_bottom_rows=base_bottom_rows,
                panel_rows=panel_rows,
                panel_ansi=panel_ansi,
                bottom_elements=tuple(bottom_elements),
                input_rows=tuple(input_rows),
            )

        return Group(*elements)

    def _active_thinking_stream_elements(self, width: int) -> list[Text]:
        lines = dock.active_thinking_stream_lines(width)
        if not lines:
            return []
        return self._transcript_elements_for_rows(lines, width, len(lines))

    def _safe_text_from_line(self, line: str) -> Text:
        try:
            return text_from_line(line)
        except Exception:
            return Text(line)

    def _visual_row_count(self, rendered: Text, width: int) -> int:
        return max(len(rendered.wrap(self._console, max(width, 1), overflow="fold")), 1)

    def _bounded_text_candidates_for_rows(
        self,
        lines: list[str],
        width: int,
        row_limit: int,
        *,
        overscan_rows: int | None = None,
    ) -> tuple[list[Text], str, bool]:
        if not lines or row_limit <= 0:
            return [], "", False

        overscan = (
            min(max(row_limit, 8), 32)
            if overscan_rows is None
            else max(overscan_rows, 0)
        )
        target_rows = row_limit + overscan
        renderables: list[Text] = []
        visual_rows = 0
        for line in reversed(lines):
            rendered = self._safe_text_from_line(line)
            renderables.insert(0, rendered)
            visual_rows += self._visual_row_count(rendered, width)
            if visual_rows >= target_rows:
                break

        ansi = self._capture_renderable(Group(*renderables), width)
        return renderables, ansi, len(renderables) < len(lines)

    def _text_elements_from_bounded_ansi(
        self,
        ansi: str,
        row_limit: int,
        *,
        truncated: bool,
        prepend_ellipsis: bool,
    ) -> list[Text]:
        if not ansi or row_limit <= 0:
            return []
        rows = ansi.splitlines()
        truncated = truncated or len(rows) > row_limit
        if prepend_ellipsis and truncated:
            if row_limit == 1:
                return [Text("…", style="dim")]
            rows = rows[-(row_limit - 1):]
            return [Text("…", style="dim")] + [Text.from_ansi(row) for row in rows]
        return [Text.from_ansi(row) for row in rows[-row_limit:]]

    def _bounded_text_elements_for_rows(
        self,
        lines: list[str],
        width: int,
        row_limit: int,
        *,
        overscan_rows: int | None = None,
        prepend_ellipsis: bool = False,
    ) -> list[Text]:
        renderables, ansi, truncated = self._bounded_text_candidates_for_rows(
            lines,
            width,
            row_limit,
            overscan_rows=overscan_rows,
        )
        del renderables
        return self._text_elements_from_bounded_ansi(
            ansi,
            row_limit,
            truncated=truncated,
            prepend_ellipsis=prepend_ellipsis,
        )

    def _transcript_elements_for_rows(
        self,
        lines: list[str],
        width: int,
        row_limit: int,
    ) -> list[Text]:
        return self._bounded_text_elements_for_rows(lines, width, row_limit)

    def _render_bottom_impl(self) -> Group:
        width = self._frame_width()
        return Group(
            *self._render_bottom_elements(
                width,
                self._render_panel_lines(width),
                self._render_hint_lines(),
            )
        )

    def _render_input_elements(self, width: int) -> list[Text]:
        elements: list[Text] = []
        prompt = "❯ "

        if self._active_text_prompt is not None:
            elements.append(Text(f"{self._active_text_prompt} ", style="bold"))
            prompt = ""

        prompt_width = 2
        for row, line in enumerate(self._input_lines):
            prefix = prompt if row == 0 else " " * prompt_width
            elements.extend(self._render_input_line(row, line, prefix, width))

        return elements

    def _render_bottom_elements(
        self,
        width: int,
        panel_lines: list[str],
        status_lines: list,
        *,
        panel_ansi: str | None = None,
        input_elements: list[Text] | None = None,
    ) -> list:
        elements: list = [Text("─" * width, style="dim")]
        if input_elements is None:
            input_elements = self._render_input_elements(width)
        elements.extend(input_elements)
        elements.append(Text("─" * width, style="dim"))

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

    def _base_bottom_elements(
        self,
        width: int,
        status_lines: list,
        *,
        input_elements: list[Text] | None = None,
    ) -> list:
        if input_elements is None:
            input_elements = self._render_input_elements(width)
        return [
            Text("─" * width, style="dim"),
            *input_elements,
            Text("─" * width, style="dim"),
            *status_lines,
        ]

    def _base_bottom_row_count(
        self,
        width: int,
        status_lines: list,
        *,
        input_elements: list[Text] | None = None,
    ) -> int:
        elements = self._base_bottom_elements(
            width,
            status_lines,
            input_elements=input_elements,
        )
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
        row_limit = self._panel_row_limit
        if row_limit is None:
            return [self._safe_text_from_line(line) for line in panel_lines]
        if row_limit <= 0:
            return []
        if panel_ansi is None:
            return self._bounded_text_elements_for_rows(
                panel_lines,
                width,
                row_limit,
                prepend_ellipsis=True,
            )
        return [Text.from_ansi(row) for row in panel_ansi.splitlines()]

    def _panel_row_count_and_ansi(
        self,
        panel_lines: list[str],
        width: int,
    ) -> tuple[int, str | None]:
        if not panel_lines:
            return 0, None
        row_limit = self._panel_row_limit
        if row_limit is None:
            elements = [self._safe_text_from_line(line) for line in panel_lines]
            if not elements:
                return 0, ""
            ansi = self._capture_renderable(Group(*elements), width)
            return _rendered_row_count(ansi), ansi
        if row_limit <= 0:
            return 0, ""

        _, ansi, truncated = self._bounded_text_candidates_for_rows(
            panel_lines,
            width,
            row_limit,
        )
        if not ansi:
            return 0, ""

        rows = ansi.splitlines()
        truncated = truncated or len(rows) > row_limit
        if truncated:
            if row_limit == 1:
                ansi = "\x1b[2m…\x1b[0m"
            else:
                visible_rows = rows[-(row_limit - 1):]
                ansi = "\x1b[2m…\x1b[0m\n" + "\n".join(visible_rows)
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
