"""Terminal rendering — ANSI output, cursor positioning, status bar."""

from __future__ import annotations

import io
import shutil
import sys
import time
from dataclasses import dataclass


from rich.cells import cell_len
from rich.console import Console, Group
from rich.text import Text

from voidx.llm.usage import format_cache_hit_rate, format_token_count
from voidx.ui.output.dock import active_agent_step_text, dock
from voidx.ui.output.dock.formatting import _text_from_line
from voidx.ui.tui.activity import (
    BUSY_ACTIVITY_DEFAULT_VERB,
    BUSY_ACTIVITY_GLYPHS,
    BUSY_ACTIVITY_GLYPH_STYLES,
    BUSY_ACTIVITY_STYLE,
)
from voidx.ui.tui.helpers import _clip_cells, _rendered_row_count
from voidx.ui.tui.overlays import _OverlayRendererMixin
from voidx.ui.tui.state import StatusSummaryCache


@dataclass(frozen=True)
class StatusSegment:
    kind: str
    text: str


_STATUS_STYLES = {
    "model": "#6CB6FF",
    "policy": "#57AB5A",
    "state": BUSY_ACTIVITY_STYLE,
    "usage": "#56D4DD",
    "goal": "#C698F0",
    "separator": "#4B5563",
}
_STATUS_VARIANTS = (
    ("model", "policy", "state", "usage", "goal"),
    ("model", "policy", "usage", "goal"),
    ("model", "policy", "usage"),
    ("model", "policy"),
    ("model",),
)
_TODO_PINNED_MAX_ITEMS = 4
_TODO_PINNED_ORDER = ("in_progress", "pending", "completed", "cancelled")
_TODO_PINNED_ICONS = {
    "pending": "○",
    "in_progress": "◐",
    "completed": "●",
    "cancelled": "✕",
}
_TODO_PINNED_STYLES = {
    "pending": "#8F9BA8",
    "in_progress": "#7AA2F7",
    "completed": "#A3BE8C",
    "cancelled": "#BF616A",
}


class _TerminalRendererMixin(_OverlayRendererMixin):
    """Methods: _render_frame, _render_input_region, _capture_renderable,
    _move_to_frame_top_sequence, _move_to_frame_end_sequence, _position_input_cursor,
    _render_impl, _render_bottom_impl, _render_bottom_elements, _render_hint_lines,
    _status_summary."""

    # ── rendering ────────────────────────────────────────────────────────

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

    def _pinned_todo_max_rows(self, render_height: int, bottom_fixed_lines: int) -> int:
        if dock.todo_state() is None:
            return 0
        available_rows = render_height - bottom_fixed_lines
        row_budget = 1 + _TODO_PINNED_MAX_ITEMS
        return max(1, min(row_budget, available_rows))

    def _pinned_todo_row_count(self, width: int, max_rows: int | None = None) -> int:
        return len(self._render_pinned_todo_elements(width, max_rows=max_rows))

    def _busy_activity_row_count(self, width: int) -> int:
        return len(self._render_busy_activity_elements(width))

    def _render_busy_activity_elements(self, width: int) -> list[Text]:
        if not self._busy:
            return []
        return [self._busy_activity_text(width)]

    def _busy_activity_text(self, width: int) -> Text:
        label = _clip_cells(self._busy_activity_label(), width)
        if not label:
            return Text()
        text = Text()
        text.append(label[:1], style=self._busy_activity_glyph_style())
        if len(label) > 1:
            text.append(label[1:], style=BUSY_ACTIVITY_STYLE)
        return text

    def _busy_activity_glyph_style(self) -> str:
        return BUSY_ACTIVITY_GLYPH_STYLES[
            self._busy_activity_tick % len(BUSY_ACTIVITY_GLYPH_STYLES)
        ]

    def _render_pinned_todo_elements(
        self,
        width: int,
        *,
        max_rows: int | None = None,
    ) -> list[Text]:
        state = dock.todo_state()
        if state is None:
            return []

        row_limit = 1 + _TODO_PINNED_MAX_ITEMS if max_rows is None else max_rows
        if row_limit <= 0:
            return []
        elements = [
            Text(_clip_cells(f"Todo: {state.summary}", width), style="bold #A3BE8C")
        ]
        if row_limit <= 1 or not state.items:
            return elements[:row_limit]

        ordered_items = [
            item
            for status in _TODO_PINNED_ORDER
            for item in state.items
            if item.status == status
        ]
        ordered_items.extend(
            item for item in state.items if item.status not in _TODO_PINNED_ORDER
        )

        available_item_rows = row_limit - 1
        visible_count = available_item_rows
        if len(ordered_items) > available_item_rows:
            visible_count = max(available_item_rows - 1, 0)
        for item in ordered_items[:visible_count]:
            icon = _TODO_PINNED_ICONS.get(item.status, "○")
            style = _TODO_PINNED_STYLES.get(item.status, "#8F9BA8")
            elements.append(Text(_clip_cells(f"  {icon} {item.content}", width), style=style))

        omitted = len(ordered_items) - visible_count
        if omitted > 0 and len(elements) < row_limit:
            elements.append(
                Text(_clip_cells(f"  … {omitted} more todos", width), style="dim")
            )
        return elements[:row_limit]

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

    def _mark_status_summary_dirty(self) -> None:
        self._render_state.status_summary_dirty = True

    def _status_summary(self, width: int) -> str:
        cache = self._render_state.status_summary_cache
        if (
            cache is not None
            and cache.width == width
            and not self._render_state.status_summary_dirty
        ):
            return cache.summary

        snapshot, segments = self._status_segments(include_busy=True)
        summary, selected = self._select_status_variant(width, segments)
        self._render_state.status_summary_dirty = False
        self._render_state.status_summary_cache = StatusSummaryCache(
            width,
            snapshot,
            summary,
            tuple((segment.kind, segment.text) for segment in selected),
        )
        return summary

    def _status_summary_text(self, width: int) -> Text:
        summary = self._status_summary(width)
        if not summary:
            return Text()
        cache = self._render_state.status_summary_cache
        selected = ()
        if cache is not None and cache.width == width and cache.summary == summary:
            selected = tuple(
                StatusSegment(kind, text)
                for kind, text in cache.segments
            )
        return self._status_text_from_segments(summary, selected)

    def _status_segments(self, *, include_busy: bool) -> tuple[tuple, tuple[StatusSegment, ...]]:
        from voidx.ui.tui.helpers import (
            _call_bool,
            _call_int,
            _call_status,
            _safe_status_value,
        )

        provider = _safe_status_value(getattr(self.status, "provider", ""), "")
        model = _safe_status_value(getattr(self.status, "model", ""), "")
        effort = _safe_status_value(getattr(self.status, "reasoning_effort", ""), "")
        permission = _call_status(getattr(self.status, "permission_label", None), "")
        sandbox = _call_status(getattr(self.status, "sandbox_label", None), "")
        approval = _call_status(getattr(self.status, "approval_label", None), "")
        reviewer = _call_status(getattr(self.status, "approval_reviewer_label", None), "")
        mode = _call_status(getattr(self.status, "interaction_mode", None), "")
        plan = _call_bool(getattr(self.status, "plan_mode", None))
        debug = _call_bool(getattr(self.status, "debug", None))
        goal_label = _call_status(getattr(self.status, "goal_label", None), "")
        goal_status = _call_status(getattr(self.status, "goal_status", None), "idle")
        goal_phase = _call_status(getattr(self.status, "goal_phase", None), "clarify")
        goal_turns = _call_int(getattr(self.status, "goal_turn_count", None), 0)
        stats = getattr(self.status, "usage_stats", None)
        context_limit = getattr(stats, "context_limit", None) or getattr(self.status, "context_limit", 0)
        stats_snapshot = (
            context_limit,
            getattr(stats, "context_tokens", 0) if stats is not None else 0,
            getattr(stats, "total_tokens", 0) if stats is not None else 0,
            getattr(stats, "cache_hit_rate", None) if stats is not None else None,
        )
        snapshot = (
            provider,
            model,
            effort,
            permission,
            sandbox,
            approval,
            reviewer,
            mode,
            plan,
            debug,
            goal_label,
            goal_status,
            goal_phase,
            goal_turns,
            self._busy,
            stats_snapshot,
        )
        model_text = "/".join(part for part in (provider, model) if part)
        if effort:
            model_text = f"{model_text} {effort}"

        policy_parts = [part for part in (permission, sandbox, approval) if part]
        if reviewer and reviewer != "user":
            policy_parts.append(reviewer)
        policy_text = " ".join(policy_parts)

        state_parts = []
        if include_busy and self._busy:
            state_parts.append("busy")
        if mode:
            state_parts.append(mode)
        if plan:
            state_parts.append("plan")
        if debug:
            state_parts.append("debug")
        state_text = " ".join(state_parts)

        usage_text = ""
        if stats is not None:
            usage_text = (
                f"ctx {format_token_count(getattr(stats, 'context_tokens', 0))}/"
                f"{format_token_count(context_limit)}"
                f" cache {format_cache_hit_rate(stats)}"
                f" total {format_token_count(getattr(stats, 'total_tokens', 0))}"
            )

        goal_text = ""
        if goal_label or goal_status != "idle":
            goal_text = f"goal {goal_status}/{goal_phase} turns {goal_turns}"
            if goal_label:
                goal_text += f" {goal_label}"

        segments = tuple(
            segment
            for segment in (
                StatusSegment("model", model_text),
                StatusSegment("policy", policy_text),
                StatusSegment("state", state_text),
                StatusSegment("usage", usage_text),
                StatusSegment("goal", goal_text),
            )
            if segment.text
        )
        return snapshot, segments

    def _select_status_variant(
        self,
        width: int,
        segments: tuple[StatusSegment, ...],
        *,
        prefix: StatusSegment | None = None,
    ) -> tuple[str, tuple[StatusSegment, ...]]:
        by_kind = {segment.kind: segment for segment in segments}
        prefix_segments = (prefix,) if prefix is not None and prefix.text else ()

        for variant in _STATUS_VARIANTS:
            selected = tuple(
                by_kind[kind]
                for kind in variant
                if kind in by_kind
            )
            candidate = prefix_segments + selected
            if not candidate:
                return "", ()
            summary = self._status_summary_from_segments(candidate)
            if cell_len(summary) <= width:
                return summary, candidate

        fallback = prefix_segments or tuple(
            segment for segment in segments if segment.kind == "model"
        )[:1]
        if not fallback:
            return "", ()
        summary = _clip_cells(self._status_summary_from_segments(fallback), width)
        return summary, ()

    @staticmethod
    def _status_summary_from_segments(segments: tuple[StatusSegment, ...]) -> str:
        return "  " + " | ".join(segment.text for segment in segments if segment.text)

    def _status_text_from_segments(
        self,
        summary: str,
        segments: tuple[StatusSegment, ...],
    ) -> Text:
        if not summary:
            return Text()
        if not segments:
            return Text(summary, style="#8F9BA8")
        if summary != self._status_summary_from_segments(segments):
            return Text(summary, style="#8F9BA8")
        text = Text("  ")
        appended = False
        for segment in segments:
            if not segment.text:
                continue
            if appended:
                text.append(" | ", style=_STATUS_STYLES["separator"])
            text.append(segment.text, style=_STATUS_STYLES.get(segment.kind, "#8F9BA8"))
            appended = True
        return text

    def _busy_activity_label(self) -> str:
        started_at = self._busy_started_at
        glyph = BUSY_ACTIVITY_GLYPHS[
            self._busy_activity_tick % len(BUSY_ACTIVITY_GLYPHS)
        ]
        verb = self._busy_activity_verb or BUSY_ACTIVITY_DEFAULT_VERB
        if started_at is None:
            return f"{glyph} {verb}"
        elapsed = max(0, int(time.monotonic() - started_at))
        details = [self._format_elapsed(elapsed)]
        step = active_agent_step_text()
        if step:
            details.append(step)
        token_text = self._turn_token_text()
        if token_text:
            details.append(token_text)
        return f"{glyph} {verb} ({' '.join(details)})"

    def _turn_token_text(self) -> str:
        stats = getattr(self.status, "usage_stats", None)
        if stats is None:
            return ""
        turn_in = getattr(stats, "turn_input_tokens", 0)
        turn_out = getattr(stats, "turn_output_tokens", 0)
        if turn_in <= 0 and turn_out <= 0:
            return ""
        return f"↑{format_token_count(turn_in)} ↓{format_token_count(turn_out)}"

    @staticmethod
    def _format_elapsed(seconds: int) -> str:
        if seconds < 60:
            return f"{seconds}s"
        minutes, remainder = divmod(seconds, 60)
        return f"{minutes}m {remainder}s"
