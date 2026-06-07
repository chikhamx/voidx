"""Pure terminal TUI — manual ANSI rendering + raw terminal input.

Renders via Rich Console captured to string, then writes directly with
explicit cursor positioning so IME overlays appear at the right spot.
"""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from rich.console import Console, Group
from rich.text import Text

from voidx.ui.output.dock import dock
from voidx.ui.output.dock.formatting import _text_from_line
from voidx.ui.output.tree import OutputTree
from voidx.ui.tools.clipboard_image import paste_clipboard_image as paste_clipboard_image_from_system
from voidx.ui.tui.helpers import (
    _ENTER_TERMINAL_SEQUENCE,
    _EXIT_TERMINAL_SEQUENCE,
    _plain_line,
    _rendered_row_count,
)
from voidx.ui.tui.choice_mixin import _ChoicePromptMixin
from voidx.ui.tui.clipboard_mixin import _ClipboardMixin
from voidx.ui.tui.input import _InputEditorMixin
from voidx.ui.tui.parser import _InputParserMixin
from voidx.ui.tui.panels import _PanelManagerMixin
from voidx.ui.tui.renderer import _TerminalRendererMixin
from voidx.ui.tui.terminal_mixin import _TerminalLifecycleMixin
from voidx.ui.tui.text_prompt_mixin import _TextPromptMixin

SubmitHandler = Callable[[str], Awaitable[bool]]


# ── PureTui ────────────────────────────────────────────────────────────────


class PureTui(
    _InputParserMixin,
    _InputEditorMixin,
    _PanelManagerMixin,
    _ChoicePromptMixin,
    _TextPromptMixin,
    _ClipboardMixin,
    _TerminalLifecycleMixin,
    _TerminalRendererMixin,
):
    """Scrollable transcript with a fixed bottom input — pure Rich + raw stdin."""

    INPUT_HISTORY_LIMIT = 1000

    def __init__(self, status, commands: list[tuple[str, str]]) -> None:
        self.status = status
        self.commands = commands
        self._console = Console()

        # Input state
        self._input_lines: list[str] = [""]
        self._cursor_row: int = 0
        self._cursor_col: int = 0
        self._input_history: list[str] = []
        self._history_idx: int = -1
        self._history_draft: list[str] = [""]

        # Submit queue / flow
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._busy: bool = False
        self._current_submit_task: asyncio.Task[bool] | None = None
        self._current_submitted_text: str = ""
        self._submit_cancel_requested: bool = False
        self._ctrl_c_armed: bool = False
        self._ctrl_c_deadline: float = 0.0

        self._init_choice_prompt_state()
        self._init_text_prompt_state()

        # Command palette
        self._command_selected: int = 0
        self._command_panel_active: bool = False

        # File attachment palette
        self._attachment_selected: int = 0
        self._attachment_panel_suppressed_text: str = ""

        self._capture_buffer: io.StringIO | None = None
        self._capture_console: Console | None = None
        self._capture_console_key: tuple[int, int | None] | None = None

        # Quiet commands
        self._quiet_commands: list[str] = []

        # Rendering
        self._running: bool = False
        self._exit_requested: bool = False
        self._last_error: str = ""
        self._notice: str = ""
        self._pending_tb: str = ""
        self._has_rendered_frame: bool = False
        self._cursor_to_frame_top_lines: int = 0
        self._cursor_to_frame_end_lines: int = 0
        self._last_frame_rows: int = 0
        self._last_frame_start_row: int = 1
        self._last_bottom_rows: int = 0
        self._last_bottom_start_row: int = 1
        self._input_region_render_pending: bool = False
        self._attachment_matches_cache_key: tuple[str, str, int, int] | None = None

        # Scrollback flush: lines already committed to terminal history
        self._committed_line_count: int = 0
        self._visible_committed_rows: int = 0
        self._was_busy: bool = False
        self._attachment_matches_cache: list[Any] = []

        # External protocol hooks (web gateway stubs)
        self._external_request_handler: Callable[[Any], Awaitable[Any]] | None = None
        self._external_command_handler: Callable[[Any], Awaitable[Any]] | None = None

        # stdin
        self._stdin_fd: int | None = self._stdin_fileno()
        self._tty: bool = False
        self._old_termios: list | None = None

        # Buffer-boundary safety: hold truncated UTF-8 or lone ESC across reads
        self._pending_bytes: bytes = b""

        # Bracketed paste: None when not pasting, bytes accumulator during paste
        self._paste_buffer: bytes | None = None

        # Clipboard watcher state (macOS: Ctrl+V image paste)
        self._clipboard_change_count: int = -1

    # ── public API ───────────────────────────────────────────────────────

    async def run(self, on_submit: SubmitHandler) -> None:
        dock.set_refresh_callback(self.invalidate)
        dock.set_width_provider(lambda: self._console.width or 80)

        self._tty = self._stdin_fd is not None and os.isatty(self._stdin_fd)
        if self._tty:
            self._setup_terminal()
            sys.stdout.write("\x1b[2J\x1b[H")
            sys.stdout.write(_ENTER_TERMINAL_SEQUENCE)
            sys.stdout.flush()

        consumer = asyncio.create_task(self._consume(on_submit))

        try:
            self._running = True
            # Flush startup banner directly to scrollback so it never
            # appears inside the TUI frame (where it would be overwritten
            # on every render and lost on exit).
            self._flush_committed(force=True)
            self._render_frame()
            while self._running:
                if self._tty:
                    data = await self._read_input_raw()
                else:
                    data = await self._read_input_line()
                if self._process_input(data):
                    self._render_after_input()
        finally:
            self._running = False
            if self._tty:
                try:
                    _dump_transcript_log(Path(self.status.workspace), dock.tree)
                except Exception as exc:
                    print(f"Transcript log write failed: {exc}", file=sys.stderr)
            self._restore_terminal()
            if self._tty:
                sys.stdout.write(self._move_to_frame_end_sequence())
                sys.stdout.write(_EXIT_TERMINAL_SEQUENCE)
                sys.stdout.flush()
            dock.set_refresh_callback(None)
            dock.set_width_provider(None)
            consumer.cancel()
            try:
                await consumer
            except asyncio.CancelledError:
                pass

    async def run_headless(self, on_submit: SubmitHandler) -> None:
        """Run without TUI — wait forever, input comes from gateway."""
        await asyncio.Event().wait()

    def submit_external_input(self, text: str) -> None:
        """Submit text from web gateway."""
        self._queue.put_nowait(text)

    def cancel_external_input(self) -> None:
        """Cancel current submission."""
        self._submit_cancel_requested = True
        if self._current_submit_task is not None:
            self._current_submit_task.cancel()

    def set_external_request_handler(self, handler) -> None:
        self._external_request_handler = handler

    def set_external_command_handler(self, handler) -> None:
        self._external_command_handler = handler

    def show_transient_output(self, text: str, title: str = "") -> None:
        from voidx.ui.output.dock import dock
        dock.append_message(text)

    def queue_quiet_command(self, command: str) -> None:
        command = command.strip()
        if not command:
            return
        self._quiet_commands.append(command)
        self._queue.put_nowait(command)

    def consume_quiet_command(self, command: str) -> bool:
        command = command.strip()
        try:
            index = self._quiet_commands.index(command)
        except ValueError:
            return False
        del self._quiet_commands[index]
        return True

    def invalidate(self) -> None:
        if self._running:
            self._flush_committed()
            self._render_frame()

    def _flush_committed(self, *, force: bool = False) -> None:
        """Flush completed content to terminal scrollback.

        During a busy turn, only the safe settled prefix is printed to
        native scrollback.  When the agent transitions from busy → idle,
        any remaining active frame content is flushed as the final fallback.

        With ``force=True``, flush regardless of busy-state transition
        (used for startup banner).
        """
        width = self._frame_width()
        tree_lines = dock.tree.render(width)
        total = len(tree_lines)

        if force:
            flush_limit = total
        else:
            is_busy = self._busy
            was_busy = self._was_busy
            self._was_busy = is_busy
            if was_busy and not is_busy:
                flush_limit = total
            else:
                flush_limit = min(
                    dock.safe_flush_line_count(width, self._committed_line_count),
                    total,
                )

        if flush_limit <= self._committed_line_count:
            return

        flush_lines = tree_lines[self._committed_line_count:flush_limit]
        self._committed_line_count = flush_limit

        if not flush_lines:
            return

        if not self._tty:
            for line in flush_lines:
                sys.stdout.write(_plain_line(line).rstrip() + "\n")
            sys.stdout.flush()
            return

        # Clear the current frame before flushing so that input box,
        # status bar, and other UI chrome are NOT carried into scrollback.
        if self._has_rendered_frame and self._last_frame_start_row > 0:
            sys.stdout.write(f"\x1b[{self._last_frame_start_row};1H")
            sys.stdout.write("\x1b[J")
            sys.stdout.flush()

        rendered_lines: list[Text] = []
        for line in flush_lines:
            try:
                rendered_lines.append(_text_from_line(line))
            except Exception:
                rendered_lines.append(Text(line))

        flush_rows = _rendered_row_count(self._capture_renderable(Group(*rendered_lines), width))

        for rendered in rendered_lines:
            self._console.print(rendered)

        term_height = shutil.get_terminal_size().lines
        self._visible_committed_rows = min(
            term_height,
            self._visible_committed_rows + flush_rows,
        )

        sys.stdout.flush()

        # The next _render_frame will reposition and draw only the
        # (now empty) active frame + input box.

    def _render_after_input(self) -> None:
        try:
            if self._input_region_render_pending:
                self._render_input_region()
            else:
                self._render_frame()
        finally:
            self._input_region_render_pending = False

    @staticmethod
    def _drain_queue(queue: asyncio.Queue) -> None:
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    def _reset_queue_for_current_loop(self, attr: str) -> None:
        queue = getattr(self, attr)
        bound_loop = getattr(queue, "_loop", None)
        if bound_loop is not None and bound_loop is not asyncio.get_running_loop():
            setattr(self, attr, asyncio.Queue())

    # ── terminal setup ───────────────────────────────────────────────────

    # ── submit ───────────────────────────────────────────────────────────

    def _do_submit(self) -> bool:
        if self._active_text_prompt is not None:
            self._submit_text_prompt()
            return True
        if self._active_choice is not None:
            self._submit_choice_selection()
            return True

        text = self._get_input_text()
        stripped = text.strip()
        if not stripped and self._is_input_empty() and not self._notice and not self._ctrl_c_armed:
            return False
        self._reset_ctrl_c()
        if self._attachment_panel_active() and self._accept_attachment_panel_selection():
            return True
        if self._command_panel_active and self._accept_command_panel_selection():
            return True
        if stripped == "/paste":
            self._record_history(text)
            self._clear_input()
            self.paste_clipboard_image()
            return True
        if not stripped:
            self._clear_input()
            return True
        if self._busy and stripped.startswith("/guide "):
            self._record_history(text)
            self._clear_input()
            self._submit_guidance_bypass(stripped)
            return True
        self._record_history(text)
        self._clear_input()
        self._queue.put_nowait(text)
        return True

    def _submit_guidance_bypass(self, text: str) -> None:
        handler = self._external_command_handler
        if handler is None:
            self._notice = "Guidance unavailable for this session."
            self.invalidate()
            return

        async def submit() -> None:
            try:
                await handler({"kind": "guide", "text": text.removeprefix("/guide").strip()})
            except Exception as exc:
                self._last_error = str(exc)
                dock.append_error(str(exc))

        asyncio.create_task(submit())

    # ── interrupt / exit ─────────────────────────────────────────────────

    def _handle_interrupt(self) -> None:
        if self._active_text_prompt is not None:
            self._cancel_text_prompt()
            self._reset_ctrl_c()
            return
        if self._active_choice is not None:
            self._finish_choice(None)
            self._reset_ctrl_c()
            return
        if self._busy and self._current_submit_task is not None:
            if not self._current_submit_task.done():
                self._submit_cancel_requested = True
                self._current_submit_task.cancel()
            self._restore_interrupted_input()
            self._reset_ctrl_c()
            self._notice = "Interrupted. Restored last message for editing."
            self.invalidate()
            return
        if not self._is_input_empty():
            self._clear_input()
            self._reset_ctrl_c()
            self._notice = "Input cleared. Press Ctrl-C twice on empty input to exit."
            self.invalidate()
            return
        now = time.monotonic()
        if not self._ctrl_c_armed or now > self._ctrl_c_deadline:
            self._ctrl_c_armed = True
            self._ctrl_c_deadline = now + 3.0
            self._notice = "Press Ctrl-C again to exit"
            self.invalidate()
            return
        self._notice = ""
        self._request_exit()

    def _reset_ctrl_c(self) -> None:
        self._ctrl_c_armed = False
        self._ctrl_c_deadline = 0.0
        self._notice = ""

    def _restore_interrupted_input(self) -> None:
        text = self._current_submitted_text
        if not text:
            return
        self._input_lines = text.split("\n")
        self._cursor_row = len(self._input_lines) - 1
        self._cursor_col = len(self._current_line())
        self._command_panel_active = False
        self._attachment_panel_suppressed_text = ""
        self._clamp_attachment_selection()

    def _request_exit(self) -> None:
        self._running = False
        self._queue.put_nowait(None)

    def _exit_app(self) -> None:
        self._running = False

    # ── consume loop ─────────────────────────────────────────────────────

    async def _consume(self, on_submit: SubmitHandler) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                if not self._exit_requested:
                    self._exit_requested = True
                    self._exit_app()
                return

            self._busy = True
            self._last_error = ""
            self._submit_cancel_requested = False
            self._current_submitted_text = item
            self._current_submit_task = asyncio.create_task(on_submit(item))
            self.invalidate()
            try:
                keep_running = await self._current_submit_task
            except asyncio.CancelledError:
                if not self._submit_cancel_requested:
                    raise
                keep_running = True
            except Exception as exc:
                self._last_error = str(exc)
                from voidx.ui.output.events import ErrorAppended, ui_events, via_events

                if via_events():
                    ui_events.emit_direct(ErrorAppended(message=str(exc)))
                else:
                    dock.append_error(str(exc))
                keep_running = True
            finally:
                self._busy = False
                self._current_submit_task = None
                self._current_submitted_text = ""
                self._submit_cancel_requested = False
                self.invalidate()

            if not keep_running:
                self._exit_requested = True
                self._exit_app()
                return


def _dump_transcript_log(workspace: Path, tree: OutputTree, *, width: int = 120) -> None:
    """Write tree contents as plain text to .voidx/transcript.log."""
    try:
        log_dir = workspace / ".voidx"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "transcript.log"

        lines = tree.render(width)
        with open(log_path, "w", encoding="utf-8") as f:
            for line in lines:
                plain = _plain_line(line)
                stripped = plain.rstrip()
                if stripped and not all(c in ('─', ' ') for c in stripped):
                    f.write(stripped + "\n")
    except Exception as exc:
        print(f"Transcript log write failed: {exc}", file=sys.stderr)
