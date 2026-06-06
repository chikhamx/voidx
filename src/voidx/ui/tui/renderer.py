"""Terminal rendering — ANSI output, cursor positioning, status bar."""

from __future__ import annotations

import io
import shutil
import sys


from rich.cells import cell_len
from rich.console import Console, Group
from rich.text import Text

from voidx.llm.usage import format_cache_hit_rate, format_token_count
from voidx.ui.output.dock import active_agent_step_text, dock
from voidx.ui.output.dock.formatting import _text_from_line
from voidx.ui.tui.helpers import _clip_cells, _rendered_row_count
from voidx.ui.tui.overlays import _OverlayRendererMixin


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
        try:
            renderable = self._render_impl(height=term_height)
        except Exception as exc:
            import traceback

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
            self._make_room_for_frame(frame_rows, term_height)
            start_row = max(self._visible_committed_rows + 1, 1)
            sys.stdout.write(f"\x1b[{start_row};1H")
            sys.stdout.write("\x1b[J")
            sys.stdout.write(ansi)
            self._last_frame_rows = frame_rows
            self._last_frame_start_row = start_row
            self._last_bottom_rows = bottom_rows
            self._last_bottom_start_row = start_row + frame_rows - bottom_rows
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

        fixed_lines = (
            1  # transcript/input separator
            + (1 if self._active_text_prompt is not None else 0)
            + sum(self._input_display_rows(width))
            + 1  # input bottom border
            + len(panel_lines)
            + (1 if panel_lines else 0)
            + len(status_lines)
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
        if row == self._cursor_row and not self._active_choice:
            if self._active_text_secret:
                col = min(cell_len(line[: self._cursor_col]), len(display))
            else:
                col = min(self._cursor_col, len(display))
            before = display[:col]
            at = display[col : col + 1] or " "
            after = display[col + 1 :]
            if before:
                segments.append((before, "white"))
            segments.append((at, "reverse white"))
            if after:
                segments.append((after, "white"))
        else:
            segments.append((display, "white"))

        return self._wrap_input_segments(segments, width)

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
        status = self._status_summary(self._frame_width())
        if status:
            lines.append(Text(status, style="#8F9BA8"))
        if self._notice:
            lines.append(Text("  " + self._notice, style="#8F9BA8"))
        if self._last_error:
            lines.append(Text("  ⚠ " + self._last_error, style="red"))
        return lines

    def _status_summary(self, width: int) -> str:
        from voidx.ui.tui.helpers import _safe_status_value, _call_status, _call_bool, _call_int
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

        model_text = "/".join(part for part in (provider, model) if part)
        if effort:
            model_text = f"{model_text} {effort}"

        policy_parts = [part for part in (permission, sandbox, approval) if part]
        if reviewer and reviewer != "user":
            policy_parts.append(reviewer)
        policy_text = " ".join(policy_parts)

        state_parts = []
        if self._busy:
            state_parts.append("busy")
        agent_step = active_agent_step_text()
        if agent_step:
            state_parts.append(agent_step)
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
                f" in {format_token_count(getattr(stats, 'last_input_tokens', 0))}"
                f" out {format_token_count(getattr(stats, 'last_output_tokens', 0))}"
                f" total {format_token_count(getattr(stats, 'total_tokens', 0))}"
            )

        goal_text = ""
        if goal_label or goal_status != "idle":
            goal_text = f"goal {goal_status}/{goal_phase} turns {goal_turns}"
            if goal_label:
                goal_text += f" {goal_label}"

        variants = [
            [model_text, policy_text, state_text, usage_text, goal_text],
            [model_text, policy_text, usage_text, goal_text],
            [model_text, policy_text, usage_text],
            [model_text, policy_text],
            [model_text],
        ]
        for variant in variants:
            summary_text = " | ".join(part for part in variant if part)
            if not summary_text:
                return ""
            summary = "  " + summary_text
            if cell_len(summary) <= width:
                return summary
        return _clip_cells("  " + model_text, width)
