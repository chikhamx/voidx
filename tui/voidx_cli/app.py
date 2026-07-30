"""Pure terminal TUI — manual ANSI rendering + raw terminal input.

Renders via Rich Console captured to string, then writes directly with
explicit cursor positioning so IME overlays appear at the right spot.
"""

from __future__ import annotations

import asyncio
import os
import random
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from rich.console import Console, Group
from rich.markup import escape
from rich.text import Text

from voidx.logging import log_internal_error
from voidx.logging.external import install_external_log_bridge
from voidx.paths import voidx_workspace_dir
from voidx.ui.output.dock import dock
from voidx.ui.output.dock.formatting import text_from_line
from voidx.ui.output.tree import OutputTree
from voidx.ui.output.types import SubmitHandler, ThreadExecutionContext, coding_turn_context_for_queue
from .helpers import (
    _ENTER_TERMINAL_SEQUENCE,
    _EXIT_TERMINAL_SEQUENCE,
    _plain_line,
    _rendered_row_count,
)
from . import activity as tui_activity
from .choice_mixin import _ChoicePromptMixin
from .clipboard_mixin import _ClipboardMixin
from .input import _InputEditorMixin
from .parser import _InputParserMixin
from .panels import _PanelManagerMixin
from .renderer import _TerminalRendererMixin
from .state import (
    CaptureState,
    ChoiceState,
    ExternalState,
    InputState,
    PanelState,
    PasteState,
    RenderState,
    STATE_FIELD_MAP,
    SubmitState,
    TerminalState,
    TextPromptState,
)
from .terminal_mixin import _TerminalLifecycleMixin
from .text_prompt_mixin import _TextPromptMixin


def _guidance_echo_lines(echoes: list[str]) -> list[str]:
    lines: list[str] = []
    for echo in echoes:
        text = echo.strip("\n")
        if not text.strip():
            continue
        echo_lines = text.splitlines()
        first = echo_lines[0] if echo_lines else ""
        header = f"[bold white]⚡[/] {escape(first)}" if first else "[bold white]⚡[/]"
        lines.append(header)
        lines.extend(escape(line) for line in echo_lines[1:])
    return lines


class _SubmitQueueItem(str):
    def __new__(
        cls,
        submit_text: str,
        *,
        restore_text: str,
        paste_entries: list[dict[str, Any]],
        thread_id: str = "",
        context: ThreadExecutionContext | None = None,
    ):
        context = context or ThreadExecutionContext(thread_id=thread_id, session_id=thread_id)
        obj = str.__new__(cls, submit_text)
        obj.restore_text = restore_text
        obj.paste_entries = [dict(entry) for entry in paste_entries]
        obj.context = context
        obj.thread_id = context.thread_id
        return obj


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
    RENDER_THROTTLE_SECONDS = 0.016

    def __getattr__(self, name: str) -> Any:
        mapping = STATE_FIELD_MAP.get(name)
        if mapping is None:
            raise AttributeError(name)
        state_attr, field_name = mapping
        state = object.__getattribute__(self, state_attr)
        return getattr(state, field_name)

    def __setattr__(self, name: str, value: Any) -> None:
        mapping = STATE_FIELD_MAP.get(name)
        if mapping is not None:
            state_attr, field_name = mapping
            try:
                state = object.__getattribute__(self, state_attr)
            except AttributeError:
                pass
            else:
                setattr(state, field_name, value)
                return
        object.__setattr__(self, name, value)

    def __init__(self, status, commands: list[tuple[str, str]]) -> None:
        self.status = status
        self.commands = commands
        self._console = Console()
        self._input_state = InputState()
        self._submit_state = SubmitState()
        self._choice_state = ChoiceState()
        self._text_prompt_state = TextPromptState()
        self._panel_state = PanelState()
        self._capture_state = CaptureState()
        self._render_state = RenderState()
        self._external_state = ExternalState()
        self._terminal_state = TerminalState(stdin_fd=self._stdin_fileno())
        self._paste_state = PasteState()

    # ── public API ───────────────────────────────────────────────────────

    async def run(self, on_submit: SubmitHandler) -> None:
        dock.set_refresh_callback(self._on_dock_refresh)
        dock.set_width_provider(lambda: self._console.width or 80)

        self._tty = self._stdin_fd is not None and os.isatty(self._stdin_fd)
        if self._tty:
            self._setup_terminal()
            sys.stdout.write("\x1b[2J\x1b[H")
            sys.stdout.write(_ENTER_TERMINAL_SEQUENCE)
            sys.stdout.flush()

        consumer = asyncio.create_task(self._consume(on_submit))
        restore_external_logging = (
            install_external_log_bridge("langchain_openai")
            if self._tty
            else None
        )

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
            self._close_stdin_reader()
            if self._tty:
                try:
                    _dump_transcript_log(Path(self.status.workspace), dock.tree)
                except Exception as exc:
                    log_internal_error(exc, context="transcript_log_write")
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
            await self._stop_busy_activity_timer()
            if restore_external_logging is not None:
                restore_external_logging()

    async def run_headless(self, on_submit: SubmitHandler) -> None:
        """Run without TUI — consume gateway input via the submit queue."""
        await self._consume(on_submit)

    def _submit_context(
        self,
        *,
        thread_id: str = "",
        context: ThreadExecutionContext | None = None,
    ) -> ThreadExecutionContext:
        return coding_turn_context_for_queue(self.status, thread_id=thread_id, context=context)

    def submit_external_input(
        self,
        text: str,
        *,
        thread_id: str = "",
        context: ThreadExecutionContext | None = None,
    ) -> None:
        """Submit text from web gateway."""
        context = self._submit_context(thread_id=thread_id, context=context)
        self._queue.put_nowait(_SubmitQueueItem(
            text,
            restore_text=text,
            paste_entries=[],
            context=context,
        ))

    def invalidate_skill_service_cache(self) -> None:
        self._skill_matches_cache_key = None
        self._skill_matches_cache = []
        self._skill_service_cache_key = None
        self._skill_service_cache = None

    def cancel_external_input(
        self,
        *,
        thread_id: str = "",
        context: ThreadExecutionContext | None = None,
    ) -> None:
        """Cancel current submission."""
        self._submit_cancel_requested = True
        if self._current_submit_task is not None:
            self._current_submit_task.cancel()

    def set_external_request_handler(self, handler) -> None:
        self._external_request_handler = handler

    def set_external_command_handler(self, handler) -> None:
        self._external_command_handler = handler

    def set_mcp_catalog_provider(self, provider) -> None:
        self._mcp_catalog_provider = provider

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
        self._mark_status_summary_dirty()
        if self._running:
            if self._render_scheduled:
                return
            self._render_scheduled = True
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                self._run_scheduled_render()
                return
            loop.call_later(self.RENDER_THROTTLE_SECONDS, self._run_scheduled_render)

    def _run_scheduled_render(self) -> None:
        self._render_scheduled = False
        if not self._running:
            return
        self._flush_committed()
        self._render_frame()

    def _choose_busy_activity_verb(self) -> str:
        return random.choice(tui_activity.BUSY_ACTIVITY_VERBS)

    def _on_dock_refresh(self) -> None:
        # A loop-waiting record may land after the turn already ended; the
        # countdown timer must be (re)started from here, not just at turn end.
        self._start_busy_activity_timer()
        self.invalidate()

    def _start_busy_activity_timer(self) -> None:
        if not self._tty or not self._running:
            return
        if not (self._busy or self._loop_waiting_active() or self._loop_turn_in_progress()):
            return
        task = self._busy_activity_timer_task
        if task is not None and not task.done():
            return
        try:
            self._busy_activity_timer_task = asyncio.create_task(self._busy_activity_timer())
        except RuntimeError:
            return

    async def _stop_busy_activity_timer(self) -> None:
        task = self._busy_activity_timer_task
        if task is None:
            return
        self._busy_activity_timer_task = None
        if task is asyncio.current_task():
            return
        try:
            task.cancel()
        except RuntimeError:
            return
        try:
            await task
        except asyncio.CancelledError:
            pass
        except RuntimeError:
            pass

    async def _busy_activity_timer(self) -> None:
        try:
            while self._running and (self._busy or self._loop_waiting_active() or self._loop_turn_in_progress()):
                await asyncio.sleep(tui_activity.BUSY_ACTIVITY_TICK_SECONDS)
                if not self._running or not (self._busy or self._loop_waiting_active() or self._loop_turn_in_progress()):
                    return
                self._busy_activity_tick += 1
                if not self._render_busy_activity_tick():
                    self._render_frame()
        except asyncio.CancelledError:
            raise

    def _flush_committed(self, *, force: bool = False) -> None:
        """Flush completed content to terminal scrollback.

        During a busy turn, only the safe settled prefix is printed to
        native scrollback.  When the agent transitions from busy → idle,
        any remaining active frame content is flushed as the final fallback.

        With ``force=True``, flush regardless of busy-state transition
        (used for startup banner).
        """
        if dock.consume_force_flush_request():
            force = True
        width = self._frame_width()
        echo_lines = _guidance_echo_lines(dock.consume_guidance_echoes())
        tree_lines = dock.tree.render(width)
        total = len(tree_lines)

        # After a dock.reset() the tree shrinks below the old committed
        # count.  If a transient node was removed, keep already-flushed
        # history committed instead of replaying it from the top.
        if self._committed_line_count > total:
            self._committed_line_count = total

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

        if flush_limit <= self._committed_line_count and not echo_lines:
            return

        flush_lines = tree_lines[self._committed_line_count:flush_limit]
        self._committed_line_count = flush_limit

        if not flush_lines and not echo_lines:
            return

        if not self._tty:
            for line in echo_lines:
                sys.stdout.write(_plain_line(line).rstrip() + "\n")
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
        for line in [*echo_lines, *flush_lines]:
            try:
                rendered_lines.append(text_from_line(line))
            except Exception:
                rendered_lines.append(Text(line))

        flush_rows = max(
            _rendered_row_count(self._capture_renderable(Group(*rendered_lines), width)),
            len(echo_lines) + len(flush_lines),
        )

        for rendered in rendered_lines:
            self._console.print(rendered)

        term_height = shutil.get_terminal_size().lines
        self._visible_committed_rows = min(
            term_height,
            self._visible_committed_rows + flush_rows,
        )
        self._invalidate_frame_cache()

        sys.stdout.flush()

        # The next _render_frame will reposition and draw only the
        # (now empty) active frame + input box.

    def _render_after_input(self) -> None:
        try:
            if (
                self._choice_selection_render_pending
                and self._render_choice_selection_region()
            ):
                return
            if self._input_region_render_pending:
                self._render_input_region()
            else:
                self._render_frame()
        finally:
            self._choice_selection_render_pending = False
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

        draft_text = self._get_input_text()
        stripped = draft_text.strip()
        if not stripped and self._is_input_empty() and not self._notice and not self._ctrl_c_armed:
            return False
        self._reset_ctrl_c()
        if self._skill_panel_active() and self._accept_skill_panel_selection():
            return True
        if self._attachment_panel_active() and self._accept_attachment_panel_selection():
            return True
        if self._command_panel_active and self._accept_command_panel_selection():
            return True
        if stripped.startswith("/"):
            head = stripped.split(None, 1)[0]
            if not any(n == head for n, _ in self.commands):
                self._clear_input()
                return True
        if stripped == "/paste":
            self._record_history(draft_text)
            self._clear_input()
            self.paste_clipboard_image()
            return True
        if not stripped:
            self._clear_input()
            return True
        if self._busy and stripped == "/clear":
            paste_entries = self._paste_entries_snapshot()
            self._record_history(draft_text, paste_entries)
            self._clear_input()
            self._drain_queue(self._queue)
            self._queue.put_nowait(_SubmitQueueItem(
                "/clear",
                restore_text=draft_text,
                paste_entries=paste_entries,
                context=self._submit_context(),
            ))
            self._submit_cancel_requested = True
            if self._current_submit_task is not None and not self._current_submit_task.done():
                self._current_submit_task.cancel()
            self._notice = "Clearing current turn..."
            self.invalidate()
            return True
        if self._busy and stripped.startswith("/guide "):
            paste_entries = self._paste_entries_snapshot()
            self._record_history(draft_text, paste_entries)
            expanded_guidance = self._expand_registered_tokens(draft_text).strip()
            self._clear_input()
            self._submit_guidance_bypass(expanded_guidance)
            return True
        submit_text = self._expand_registered_tokens(draft_text)
        paste_entries = self._paste_entries_snapshot()
        self._record_history(draft_text, paste_entries)
        self._clear_input()
        self._queue.put_nowait(_SubmitQueueItem(
            submit_text,
            restore_text=draft_text,
            paste_entries=paste_entries,
            context=self._submit_context(),
        ))
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
        loop_active = self._loop_turn_in_progress() or self._loop_waiting_active()
        if not self._is_input_empty():
            self._clear_input()
            self._reset_ctrl_c()
            self._notice = "Input cleared. Press Ctrl-C twice on empty input to exit."
            self.invalidate()
            return
        if loop_active:
            if self._active_text_prompt is not None:
                self._cancel_text_prompt()
            if self._active_choice is not None:
                self._finish_choice(None)
            self._queue.put_nowait(_SubmitQueueItem(
                "/loop stop",
                restore_text="",
                paste_entries=[],
                context=self._submit_context(),
            ))
            self._reset_ctrl_c()
            self._notice = "Stopping loop..."
            self.invalidate()
            return
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
        self._restore_paste_entries(self._current_submitted_paste_entries)
        self._command_panel_active = False
        self._attachment_panel_suppressed_text = ""
        self._attachment_matches_cache_key = None
        self._skill_panel_suppressed_text = ""
        self._skill_matches_cache_key = None
        self._clamp_attachment_selection()
        self._clamp_skill_selection()

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

            submit_text = str(item)
            restore_text = getattr(item, "restore_text", submit_text)
            paste_entries = getattr(item, "paste_entries", [])
            context = getattr(item, "context", None)
            if context is None:
                thread_id = getattr(item, "thread_id", "")
                context = self._submit_context(thread_id=thread_id)

            self._busy = True
            self._busy_started_at = time.monotonic()
            self._busy_activity_verb = self._choose_busy_activity_verb()
            self._busy_activity_prev_has_special = False
            self._busy_activity_tick = 0
            self._last_error = ""
            self._submit_cancel_requested = False
            self._current_submitted_text = restore_text
            self._current_submitted_paste_entries = [dict(entry) for entry in paste_entries]
            try:
                submit_result = on_submit(submit_text, context=context)
            except TypeError:
                try:
                    submit_result = on_submit(submit_text, thread_id=context.thread_id)
                except TypeError:
                    submit_result = on_submit(submit_text)
            self._current_submit_task = asyncio.create_task(submit_result)
            self._start_busy_activity_timer()
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
                self._busy_started_at = None
                self._busy_activity_verb = ""
                self._busy_activity_tick = 0
                self._busy_activity_prev_has_special = False
                await self._stop_busy_activity_timer()
                # A loop may still be waiting for its next wakeup: keep the
                # countdown ticking after the turn ends.
                self._start_busy_activity_timer()
                self._current_submit_task = None
                self._current_submitted_text = ""
                self._current_submitted_paste_entries = []
                self._submit_cancel_requested = False
                self.invalidate()

            if not keep_running:
                self._exit_requested = True
                self._exit_app()
                return


def _dump_transcript_log(workspace: Path, tree: OutputTree, *, width: int = 120) -> None:
    """Write tree contents as plain text to .voidx/transcript.log."""
    try:
        log_dir = voidx_workspace_dir(workspace)
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
        log_internal_error(exc, context="transcript_log_write")
