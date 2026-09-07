"""Pure terminal TUI — manual ANSI rendering + raw terminal input.

Renders via Rich Console captured to string, then writes directly with
explicit cursor positioning so IME overlays appear at the right spot.
"""

from __future__ import annotations

import asyncio
import threading
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

from voidx.observability import log_internal_error
from voidx.observability.external import install_external_log_bridge
from voidx.platform.paths import voidx_workspace_dir
from voidx.presentation.output.dock import dock
from voidx.presentation.output.dock.formatting import text_from_line
from voidx.presentation.output.tree import OutputTree
from voidx.presentation.output.types import SubmitHandler, ThreadExecutionContext, coding_turn_context_for_queue
from .async_utils import await_cancellation_safe
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
    CommittedProjection,
    STATE_FIELD_MAP,
    SubmitState,
    TerminalState,
    TextPromptState,
)
from .terminal_mixin import _TerminalLifecycleMixin
from .terminal_writer import BatchToken, TerminalWriter
from .text_prompt_mixin import _TextPromptMixin


TRANSCRIPT_EXPORT_TIMEOUT_SECONDS = 2.0

async def _dump_transcript_log_with_timeout(
    workspace: Path,
    tree: OutputTree,
    *,
    timeout: float,
) -> None:
    loop = asyncio.get_running_loop()
    completion: asyncio.Future[BaseException | None] = loop.create_future()

    def finish(error: BaseException | None) -> None:
        if not completion.done():
            completion.set_result(error)

    def publish(error: BaseException | None) -> None:
        try:
            loop.call_soon_threadsafe(finish, error)
        except RuntimeError:
            return

    def worker() -> None:
        try:
            _dump_transcript_log(workspace, tree)
        except BaseException as exc:
            publish(exc)
        else:
            publish(None)

    thread = threading.Thread(
        target=worker,
        name="voidx-transcript-export",
        daemon=True,
    )
    thread.start()
    try:
        error = await asyncio.wait_for(asyncio.shield(completion), timeout=timeout)
    except asyncio.TimeoutError:
        log_internal_error(
            TimeoutError(f"transcript export exceeded {timeout:.2f}s timeout"),
            context="transcript_log_timeout",
        )
    else:
        if error is not None:
            log_internal_error(error, context="transcript_log_write")



def _stream_is_tty(stream: Any) -> bool:
    try:
        return bool(stream.isatty())
    except Exception:
        return False


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
    LIVE_HISTORY_ROOT_TURN_LIMIT = 20
    LIVE_HISTORY_PROJECTED_BODY_LIMIT = 16 * 1024 * 1024

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
        self._terminal_writer = TerminalWriter()
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
        terminal_setup_attempted = False
        writer_started = False
        startup_completed = False
        consumer: asyncio.Task[None] | None = None
        restore_external_logging = None
        writer_failed_event = asyncio.Event()
        writer_error: Exception | None = None
        primary_error: BaseException | None = None
        cleanup_error: BaseException | None = None
        cleanup_cancellation: asyncio.CancelledError | None = None
        self._terminal_writer_failed = False
        self._terminal_writer_required = False

        def record_terminal_cleanup_error(
            exc: BaseException,
            *,
            context: str,
        ) -> None:
            nonlocal cleanup_error
            log_internal_error(exc, context=context)
            if cleanup_error is None:
                cleanup_error = exc

        def handle_writer_error(exc: Exception) -> None:
            nonlocal writer_error, cleanup_error
            if writer_error is not None:
                return
            writer_error = exc
            if cleanup_error is None:
                cleanup_error = exc
            self._terminal_writer_failed = True
            self._invalidate_layout("writer_error")
            self._running = False
            writer_failed_event.set()

        async def read_tty_input() -> bytes:
            input_task = asyncio.create_task(self._read_input_raw())
            failure_task = asyncio.create_task(writer_failed_event.wait())
            try:
                await asyncio.wait(
                    {input_task, failure_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if writer_failed_event.is_set():
                    input_task.cancel()
                    await asyncio.gather(input_task, return_exceptions=True)
                    failure_task.cancel()
                    await asyncio.gather(failure_task, return_exceptions=True)
                    if writer_error is None:
                        raise RuntimeError("terminal writer failed")
                    raise writer_error

                failure_task.cancel()
                await asyncio.gather(failure_task, return_exceptions=True)
                return input_task.result()
            finally:
                for task in (input_task, failure_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(
                    input_task,
                    failure_task,
                    return_exceptions=True,
                )

        try:
            dock.set_refresh_callback(self._on_dock_refresh)
            dock.set_width_provider(lambda: self._console.width or 80)
            self._tty = (
                self._stdin_fd is not None
                and os.isatty(self._stdin_fd)
                and _stream_is_tty(sys.stdout)
            )
            self._terminal_writer_required = self._tty
            if self._tty:
                terminal_setup_attempted = True
                self._setup_terminal()
                self._terminal_writer.start(
                    loop=asyncio.get_running_loop(),
                    on_frame_result=self._handle_terminal_frame_result,
                    on_error=handle_writer_error,
                )
                writer_started = True
                startup = self._terminal_writer.submit_barrier(
                    kind="startup",
                    ansi="\x1b[2J\x1b[H" + _ENTER_TERMINAL_SEQUENCE,
                )
                await self._terminal_writer.wait(startup)
                startup_completed = True

            consumer = asyncio.create_task(self._consume(on_submit))
            restore_external_logging = (
                install_external_log_bridge("langchain_openai")
                if self._tty
                else None
            )
            self._running = True
            self._flush_committed(force=True)
            self._render_frame()
            while self._running:
                data = (
                    await read_tty_input()
                    if self._tty
                    else await self._read_input_line()
                )
                if self._process_input(data):
                    self._render_after_input()
        except BaseException as exc:
            primary_error = exc
        finally:
            async def cleanup() -> None:
                self._running = False
                try:
                    self._close_stdin_reader()
                except BaseException as exc:
                    log_internal_error(exc, context="terminal_stdin_close")

                if consumer is not None:
                    consumer.cancel()
                    try:
                        await consumer
                    except asyncio.CancelledError:
                        pass
                    except BaseException as exc:
                        log_internal_error(exc, context="terminal_consumer_stop")
                try:
                    await self._stop_panel_query_tasks()
                except BaseException as exc:
                    log_internal_error(exc, context="terminal_panel_query_stop")

                try:
                    await self._stop_busy_activity_timer()
                except BaseException as exc:
                    log_internal_error(exc, context="terminal_busy_timer_stop")

                writer_healthy = writer_started and not self._terminal_writer_failed
                if self._tty and writer_healthy:
                    try:
                        await self._drain_committed_output()
                    except BaseException as exc:
                        writer_healthy = False
                        record_terminal_cleanup_error(
                            exc,
                            context="terminal_commit_drain",
                        )

                if terminal_setup_attempted:
                    try:
                        self._restore_terminal()
                    except BaseException as exc:
                        record_terminal_cleanup_error(
                            exc,
                            context="terminal_restore",
                        )

                if (
                    self._tty
                    and writer_healthy
                    and startup_completed
                    and not self._terminal_writer_failed
                ):
                    try:
                        restore = self._terminal_writer.submit_barrier(
                            kind="restore",
                            ansi=self._move_to_frame_end_sequence()
                            + _EXIT_TERMINAL_SEQUENCE,
                        )
                        await self._terminal_writer.wait(restore)
                    except BaseException as exc:
                        writer_healthy = False
                        record_terminal_cleanup_error(
                            exc,
                            context="terminal_exit_restore",
                        )

                writer_shutdown_succeeded = not writer_started
                if writer_started:
                    try:
                        await self._terminal_writer.shutdown_async()
                    except BaseException as exc:
                        record_terminal_cleanup_error(
                            exc,
                            context="terminal_writer_shutdown",
                        )
                    else:
                        writer_shutdown_succeeded = True

                if self._tty and writer_shutdown_succeeded:
                    try:
                        await _dump_transcript_log_with_timeout(
                            Path(self.status.workspace),
                            dock.tree,
                            timeout=TRANSCRIPT_EXPORT_TIMEOUT_SECONDS,
                        )
                    except BaseException as exc:
                        log_internal_error(exc, context="transcript_log_write")

                dock.set_refresh_callback(None)
                dock.set_width_provider(None)
                if restore_external_logging is not None:
                    try:
                        restore_external_logging()
                    except BaseException as exc:
                        log_internal_error(exc, context="external_log_restore")
                self._terminal_writer_required = False

            _, cleanup_cancellation = await await_cancellation_safe(cleanup())

        if primary_error is not None:
            raise primary_error
        if cleanup_cancellation is not None:
            raise cleanup_cancellation
        if cleanup_error is not None:
            raise cleanup_error

    async def run_headless(self, on_submit: SubmitHandler) -> None:
        """Run without TUI — consume gateway input via the submit queue."""
        await self._consume(on_submit)

    def _lock_submit_context_from_runtime_state(self) -> None:
        if self._locked_submit_context is not None:
            return
        if not bool(dock):
            return
        if self._loop_waiting_active():
            metadata = getattr(dock, "last_turn_metadata", None)
        elif getattr(dock, "turn_in_progress", False):
            metadata = getattr(dock, "current_turn_metadata", None)
        else:
            return
        context = getattr(metadata, "context", None)
        if context is None:
            return
        self._locked_submit_context = context
        self._locked_submit_context_explicit = True

    def _lock_submit_context_for_profile(self, profile_id: str) -> None:
        del profile_id
        self._locked_submit_context = coding_turn_context_for_queue(self.status)
        self._locked_submit_context_explicit = True

    def _submit_context(
        self,
        *,
        thread_id: str = "",
        context: ThreadExecutionContext | None = None,
        lock_from_runtime_state: bool = True,
    ) -> ThreadExecutionContext:
        if context is None:
            if lock_from_runtime_state:
                self._lock_submit_context_from_runtime_state()
            context = self._locked_submit_context
            if context is not None and not self._locked_submit_context_explicit:
                current = coding_turn_context_for_queue(self.status, thread_id=thread_id)
                current_profile = current.runtime_profile.profile_id
                locked_profile = context.runtime_profile.profile_id
                if current.session_id != context.session_id or current_profile != locked_profile:
                    context = current
                    self._locked_submit_context = current
        return coding_turn_context_for_queue(self.status, thread_id=thread_id, context=context)

    def submit_external_input(
        self,
        text: str,
        *,
        thread_id: str = "",
        context: ThreadExecutionContext | None = None,
    ) -> None:
        """Submit text from web gateway."""
        explicit_context = context is not None
        context = self._submit_context(
            thread_id=thread_id,
            context=context,
            lock_from_runtime_state=explicit_context or not text.strip().startswith("/"),
        )
        if explicit_context:
            self._locked_submit_context = context
            self._locked_submit_context_explicit = True
        elif self._locked_submit_context is None and not text.strip().startswith("/"):
            self._locked_submit_context = context
            self._locked_submit_context_explicit = False
        self._queue.put_nowait(_SubmitQueueItem(
            text,
            restore_text=text,
            paste_entries=[],
            context=context,
        ))

    def invalidate_skill_service_cache(self) -> None:
        self._invalidate_skill_catalog_cache()

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
        self._invalidate_skill_catalog_cache()

    def set_skills_api_provider(self, provider) -> None:
        self._skills_api_provider = provider
        self._invalidate_skill_catalog_cache()

    def show_transient_output(self, text: str, title: str = "") -> None:
        from voidx.presentation.output.dock import dock
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

    def hide_command_output(self) -> None:
        return None

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
        restore_needs_first_frame = (
            dock.restored_root_child_range() is not None
            and not self._has_rendered_frame
        )
        self._flush_committed()
        self._render_frame()
        if restore_needs_first_frame:
            self._flush_committed()
            self._render_frame()

    def _choose_busy_activity_verb(self) -> str:
        return random.choice(tui_activity.BUSY_ACTIVITY_VERBS)

    def _on_dock_refresh(self) -> None:
        self._lock_submit_context_from_runtime_state()
        # A loop-waiting record may land after the turn already ended; the
        # countdown timer must be (re)started from here, not just at turn end.
        self._start_busy_activity_timer()
        self.invalidate()

    def _start_busy_activity_timer(self) -> None:
        if not self._tty or not self._running:
            return
        if not self._busy_activity_tick_active():
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
            while self._running and self._busy_activity_tick_active():
                await asyncio.sleep(tui_activity.BUSY_ACTIVITY_TICK_SECONDS)
                if not self._running or not self._busy_activity_tick_active():
                    return
                self._busy_activity_tick += 1
                if not self._render_busy_activity_tick():
                    self._render_frame()
        except asyncio.CancelledError:
            raise

    def _sync_restored_render_state(self) -> tuple[int, int] | None:
        restored_range = dock.restored_root_child_range()
        if restored_range is None:
            if self._restored_range_key is not None:
                self._restored_range_key = None
                self._restored_committed_line_count = 0
                self._restored_startup_flushed = False
                self._restored_history_retired = False
            return None
        start, end = restored_range
        key = (id(dock.tree), start, end)
        if self._restored_range_key != key:
            self._restored_range_key = key
            self._restored_committed_line_count = 0
            self._restored_startup_flushed = False
            self._restored_history_retired = False
            self._committed_line_count = 0
            self._committed_projection = None
            self._committed_tree_revision = -1
            self._has_rendered_frame = False
            self._invalidate_frame_cache()
        return restored_range
    async def _drain_committed_output(self) -> None:
        while True:
            commit = self._flush_committed(force=True)
            if commit is None:
                break
            pending_task = self._pending_commit_tasks.get(id(commit))
            if pending_task is not None:
                await pending_task
            else:
                await self._terminal_writer.wait(commit)
        await self._terminal_writer.drain_async()


    async def _wait_for_pending_commit(self, token: BatchToken) -> None:
        token_key = id(token)
        update = self._render_state.pending_commit_updates[token_key]
        try:
            await self._terminal_writer.wait(token)
        except BaseException as exc:
            if update["force_requested"]:
                dock.request_force_flush()
            dock.restore_guidance_echoes(update["raw_echoes"])
            if not isinstance(exc, asyncio.CancelledError):
                log_internal_error(exc, context="terminal_commit_wait")
        else:
            update["apply_state"]()
            term_height = shutil.get_terminal_size().lines
            self._visible_committed_rows = min(
                term_height,
                self._visible_committed_rows + update["flush_rows"],
            )
            self._invalidate_frame_cache()
        finally:
            self._render_state.pending_commit_updates.pop(token_key, None)
            self._render_state.pending_commit_tasks.pop(token_key, None)
            self._render_state.pending_terminal_operations.pop(token_key, None)
            try:
                self._render_state.pending_commit_tokens.remove(token)
            except ValueError:
                pass
            if self._running:
                self.invalidate()

    def _track_pending_commit(
        self,
        token: BatchToken,
        *,
        apply_state,
        flush_rows: int,
        force_requested: bool,
        raw_echoes: list[str],
    ) -> None:
        token_key = id(token)
        self._render_state.pending_terminal_operations[token_key] = {
            "kind": "commit",
            "token": token,
            "scroll_epoch": self._scroll_epoch,
            "apply_state": apply_state,
        }
        self._render_state.pending_commit_tokens.append(token)
        self._render_state.pending_commit_updates[token_key] = {
            "apply_state": apply_state,
            "flush_rows": flush_rows,
            "force_requested": force_requested,
            "raw_echoes": raw_echoes,
        }
        self._render_state.pending_commit_tasks[token_key] = asyncio.create_task(
            self._wait_for_pending_commit(token)
        )

    @staticmethod
    def _committed_node_signature(node) -> tuple[Any, ...]:
        parent = node.parent
        grandparent = parent.parent if parent is not None else None
        return (
            node.node_type,
            node.header,
            tuple(node.body_lines),
            node.collapsed,
            node.status,
            node.elapsed,
            node.agent_name,
            node.step_info,
            node.meta,
            node.tool_call_id,
            node.agent_run_id,
            node.message_id,
            parent.id if parent is not None else None,
            parent.node_type if parent is not None else None,
            parent.payload.get("tool_name") if parent is not None else None,
            grandparent.id if grandparent is not None else None,
            grandparent.payload.get("tool_name") if grandparent is not None else None,
            bool(node.payload.get("diff_text")),
            bool(node.payload.get("full_width_user_row")),
            bool(node.payload.get("align_full_width_user_row")),
            node.payload.get("phase"),
            node.payload.get("tool_name"),
        )

    @staticmethod
    def _unowned_line_keys(
        lines: list[str],
        line_map: dict[int, str],
    ) -> dict[int, tuple[str | None, str | None, int]]:
        following_owners: list[str | None] = [None] * len(lines)
        following: str | None = None
        for index in range(len(lines) - 1, -1, -1):
            owner = line_map.get(index)
            if owner is not None:
                following = owner
            following_owners[index] = following

        result: dict[int, tuple[str | None, str | None, int]] = {}
        occurrences: dict[tuple[str | None, str | None], int] = {}
        previous: str | None = None
        for index in range(len(lines)):
            owner = line_map.get(index)
            if owner is not None:
                previous = owner
                continue
            pair = (previous, following_owners[index])
            occurrence = occurrences.get(pair, 0)
            occurrences[pair] = occurrence + 1
            result[index] = (*pair, occurrence)
        return result

    def _committed_projection_for_prefix(
        self,
        lines: list[str],
        line_map: dict[int, str],
        limit: int,
    ) -> CommittedProjection:
        bounded_limit = min(max(limit, 0), len(lines))
        node_ids = {
            node_id
            for index in range(bounded_limit)
            if (node_id := line_map.get(index)) is not None
        }
        node_signatures = {
            node_id: self._committed_node_signature(node)
            for node_id in node_ids
            if (node := dock.tree.get(node_id)) is not None
        }
        unowned_keys = self._unowned_line_keys(lines, line_map)
        unowned_signatures = {
            unowned_keys[index]: lines[index]
            for index in range(bounded_limit)
            if index in unowned_keys
        }
        return CommittedProjection(
            tree=dock.tree,
            node_signatures=node_signatures,
            unowned_signatures=unowned_signatures,
        )

    def _merged_committed_projection(
        self,
        lines: list[str],
        line_map: dict[int, str],
        limit: int,
        previous: CommittedProjection | None,
    ) -> CommittedProjection:
        current = self._committed_projection_for_prefix(
            lines,
            line_map,
            limit,
        )
        if previous is None or previous.tree is not dock.tree:
            return current
        node_signatures = {
            node_id: signature
            for node_id, signature in previous.node_signatures.items()
            if dock.tree.get(node_id) is not None
        }
        node_signatures.update(current.node_signatures)
        current_gap_keys = set(self._unowned_line_keys(lines, line_map).values())
        unowned_signatures = {
            key: signature
            for key, signature in previous.unowned_signatures.items()
            if key in current_gap_keys
        }
        unowned_signatures.update(current.unowned_signatures)
        return CommittedProjection(
            tree=current.tree,
            node_signatures=node_signatures,
            unowned_signatures=unowned_signatures,
        )


    def _identity_filtered_line_indexes(
        self,
        lines: list[str],
        line_map: dict[int, str],
        limit: int,
        projection: CommittedProjection,
    ) -> list[int]:
        bounded_limit = min(max(limit, 0), len(lines))
        current_signatures: dict[str, tuple[Any, ...]] = {}
        for index in range(bounded_limit):
            node_id = line_map.get(index)
            if node_id is None or node_id in current_signatures:
                continue
            node = dock.tree.get(node_id)
            if node is not None:
                current_signatures[node_id] = self._committed_node_signature(node)
        changed_nodes = {
            node_id
            for node_id, signature in current_signatures.items()
            if projection.node_signatures.get(node_id) != signature
        }
        unowned_keys = self._unowned_line_keys(lines, line_map)
        result: list[int] = []
        for index in range(bounded_limit):
            node_id = line_map.get(index)
            if node_id is not None:
                if node_id in changed_nodes:
                    result.append(index)
                continue
            key = unowned_keys[index]
            previous, following, _occurrence = key
            if (
                projection.unowned_signatures.get(key) != lines[index]
                or previous in changed_nodes
                or following in changed_nodes
            ):
                result.append(index)
        return result

    def _active_identity_line_indexes(
        self,
        lines: list[str],
        line_map: dict[int, str],
    ) -> list[int]:
        projection = self._committed_projection
        if projection is None or projection.tree is not dock.tree:
            committed = min(self._committed_line_count, len(lines))
            return list(range(committed, len(lines)))
        return self._identity_filtered_line_indexes(
            lines,
            line_map,
            len(lines),
            projection,
        )

    @staticmethod
    def _pending_stream_flush_limit(
        line_map: dict[int, str],
        limit: int,
    ) -> int:
        bounded_limit = max(0, limit)
        pending_indexes = [
            index
            for index, node_id in line_map.items()
            if index < bounded_limit
            and (node := dock.tree.get(node_id)) is not None
            and node.payload.get("render_pending")
        ]
        return min(pending_indexes, default=bounded_limit)

    @staticmethod
    def _projected_segment_bytes(segment) -> int:
        total = 0
        stack = list(segment)
        while stack:
            node = stack.pop()
            total += len(node.header.encode("utf-8"))
            total += sum(len(line.encode("utf-8")) for line in node.body_lines)
            stack.extend(node.children)
        return total

    def _record_committed_live_history(self, width: int) -> None:
        committed = dock.tree.mark_root_turns_committed_through_line(
            width,
            self._committed_line_count,
        )
        self._committed_turn_ids.update(committed)
        self._durable_turn_ids = {
            turn_id
            for child in dock.tree.root.children
            if child.node_type == "turn"
            and isinstance((turn_id := child.payload.get("transcript_turn_id")), int)
            and not isinstance(turn_id, bool)
            and turn_id >= 0
            and child.payload.get("durable") is True
        }
        self._apply_live_history_retention(width)

    def _apply_live_history_retention(self, width: int) -> list[int]:
        tree = dock.tree
        turns = [child for child in tree.root.children if child.node_type == "turn"]
        projected_bytes = sum(
            self._projected_segment_bytes(tree.root_turn_segment(turn_id))
            for turn in turns
            if isinstance((turn_id := turn.payload.get("transcript_turn_id")), int)
            and not isinstance(turn_id, bool)
            and turn_id >= 0
        )
        removed: list[int] = []
        if (
            len(turns) <= self.LIVE_HISTORY_ROOT_TURN_LIMIT
            and projected_bytes <= self.LIVE_HISTORY_PROJECTED_BODY_LIMIT
        ):
            self._projected_body_bytes = projected_bytes
            self._retained_root_turn_count = len(turns)
            self._last_evicted_turn_ids = []
            return removed
        previous_line_count = len(
            tree.render_root_slice(width, 0, len(tree.root.children))
        )
        while len(turns) > 1 and (
            len(turns) > self.LIVE_HISTORY_ROOT_TURN_LIMIT
            or projected_bytes > self.LIVE_HISTORY_PROJECTED_BODY_LIMIT
        ):
            oldest = turns[0]
            turn_id = oldest.payload.get("transcript_turn_id")
            if not isinstance(turn_id, int) or isinstance(turn_id, bool) or turn_id < 0:
                break
            segment = tree.root_turn_segment(turn_id)
            segment_bytes = self._projected_segment_bytes(segment)
            if not tree.remove_root_turn(turn_id):
                break
            removed.append(turn_id)
            projected_bytes = max(0, projected_bytes - segment_bytes)
            turns = [child for child in tree.root.children if child.node_type == "turn"]

        if removed:
            current_line_count = len(
                tree.render_root_slice(width, 0, len(tree.root.children))
            )
            removed_lines = max(0, previous_line_count - current_line_count)
            self._committed_line_count = max(
                0,
                self._committed_line_count - removed_lines,
            )
            self._invalidate_frame_cache()
        self._projected_body_bytes = projected_bytes
        self._retained_root_turn_count = len(turns)
        self._last_evicted_turn_ids = removed
        return removed

    def _flush_committed(self, *, force: bool = False) -> BatchToken | None:
        """Flush completed content to terminal scrollback."""
        force_requested = dock.consume_force_flush_request()
        if force_requested:
            force = True
        raw_echoes = dock.consume_guidance_echoes()
        echo_lines = _guidance_echo_lines(raw_echoes)
        worker_mode = self._tty and self._terminal_writer_worker_mode()
        if worker_mode and self._render_state.pending_commit_tokens:
            if force_requested:
                dock.request_force_flush()
            dock.restore_guidance_echoes(raw_echoes)
            return self._render_state.pending_commit_tokens[-1]

        next_committed_line_count = self._committed_line_count
        next_committed_projection = self._committed_projection
        next_restored_committed_line_count = self._restored_committed_line_count
        next_restored_startup_flushed = self._restored_startup_flushed
        next_restored_history_retired = self._restored_history_retired
        next_was_busy = self._was_busy

        def apply_state() -> None:
            tree_changed_before_apply = (
                dock.tree.revision != submitted_tree_revision
            )
            self._committed_line_count = next_committed_line_count
            self._committed_projection = next_committed_projection
            self._restored_committed_line_count = next_restored_committed_line_count
            self._restored_startup_flushed = next_restored_startup_flushed
            self._restored_history_retired = next_restored_history_retired
            self._was_busy = next_was_busy
            if restored_range is None:
                self._record_committed_live_history(width)
            elif next_restored_history_retired:
                dock.retire_restored_root_child_range()
                self._restored_range_key = None
                self._restored_committed_line_count = 0
                self._restored_startup_flushed = False
                self._restored_history_retired = False
                lines, line_map = dock.tree.render_root_slice_with_line_map(
                    width,
                    0,
                    len(dock.tree.root.children),
                )
                self._committed_line_count = len(lines)
                self._committed_projection = self._committed_projection_for_prefix(
                    lines,
                    line_map,
                    len(lines),
                )
                self._record_committed_live_history(width)
            if restored_range is None and tree_changed_before_apply:
                self._committed_tree_revision = submitted_tree_revision
            else:
                self._committed_tree_revision = dock.tree.revision

        try:
            width = self._frame_width()
            restored_range = self._sync_restored_render_state()
            next_committed_line_count = self._committed_line_count
            next_committed_projection = self._committed_projection
            next_restored_committed_line_count = self._restored_committed_line_count
            next_restored_startup_flushed = self._restored_startup_flushed
            next_restored_history_retired = self._restored_history_retired
            if (
                restored_range is None
                and not echo_lines
                and dock.tree.revision == self._committed_tree_revision
            ):
                self._apply_live_history_retention(width)
                self._committed_tree_revision = dock.tree.revision
                return None


            if restored_range is not None:
                restored_start, restored_end = restored_range
                prefix_lines: list[str] = []
                if force and not next_restored_startup_flushed and restored_start:
                    prefix_lines = dock.tree.render_root_slice(width, 0, restored_start)
                    next_restored_startup_flushed = True

                current_end = len(dock.tree.root.children)
                if current_end > restored_end:
                    added_lines, added_line_map = dock.tree.render_root_slice_with_line_map(
                        width,
                        restored_end,
                        current_end,
                    )
                else:
                    added_lines, added_line_map = [], {}
                if (
                    self._tty
                    and not self._has_rendered_frame
                    and current_end > restored_end
                    and not echo_lines
                ):
                    if force_requested:
                        dock.request_force_flush()
                    return None

                committed_added = min(
                    next_restored_committed_line_count,
                    len(added_lines),
                )
                next_restored_committed_line_count = committed_added
                if force:
                    flush_limit = len(added_lines)
                else:
                    is_busy = self._busy
                    was_busy = next_was_busy
                    next_was_busy = is_busy
                    if was_busy and not is_busy:
                        flush_limit = len(added_lines)
                    else:
                        flush_limit = min(
                            dock.safe_flush_root_slice_line_count(
                                width,
                                restored_end,
                                current_end,
                                committed_added,
                            ),
                            len(added_lines),
                        )
                flush_limit = min(
                    self._pending_stream_flush_limit(added_line_map, flush_limit),
                    len(added_lines),
                )

                restored_lines: list[str] = []
                if (
                    self._tty
                    and flush_limit > committed_added
                    and not next_restored_history_retired
                ):
                    restored_lines = dock.tree.render_root_slice(
                        width,
                        restored_start if next_restored_startup_flushed else 0,
                        restored_end,
                    )

                flush_lines = [
                    *prefix_lines,
                    *restored_lines,
                    *added_lines[committed_added:flush_limit],
                ]
                next_restored_committed_line_count = flush_limit
                if flush_limit > committed_added:
                    next_restored_history_retired = True
            else:
                tree_lines, line_map = dock.tree.render_with_line_map(width)
                total = len(tree_lines)
                committed_count = min(next_committed_line_count, total)
                if force:
                    flush_limit = total
                else:
                    is_busy = self._busy
                    was_busy = next_was_busy
                    next_was_busy = is_busy
                    if was_busy and not is_busy:
                        flush_limit = total
                    else:
                        flush_limit = min(
                            dock.safe_flush_line_count(width, 0),
                            total,
                        )
                flush_limit = self._pending_stream_flush_limit(line_map, flush_limit)

                previous_projection = next_committed_projection
                if (
                    previous_projection is not None
                    and previous_projection.tree is dock.tree
                ):
                    flush_indexes = self._identity_filtered_line_indexes(
                        tree_lines,
                        line_map,
                        flush_limit,
                        previous_projection,
                    )
                    flush_lines = [tree_lines[index] for index in flush_indexes]
                else:
                    flush_lines = tree_lines[committed_count:flush_limit]
                next_committed_line_count = flush_limit
                next_committed_projection = self._merged_committed_projection(
                    tree_lines,
                    line_map,
                    flush_limit,
                    previous_projection,
                )

            submitted_tree_revision = dock.tree.revision
            if not flush_lines and not echo_lines:
                apply_state()
                return None

            if not self._tty:
                for line in echo_lines:
                    self._terminal_writer.write(_plain_line(line).rstrip() + "\n")
                for line in flush_lines:
                    self._terminal_writer.write(_plain_line(line).rstrip() + "\n")
                self._terminal_writer.flush()
                apply_state()
                return None

            rendered_lines: list[Text] = []
            for line in [*echo_lines, *flush_lines]:
                try:
                    rendered_lines.append(text_from_line(line))
                except Exception:
                    rendered_lines.append(Text(line))

            flush_ansi = self._capture_renderable(Group(*rendered_lines), width)
            commit_ansi = flush_ansi + "\n"
            flush_rows = max(
                _rendered_row_count(flush_ansi),
                len(echo_lines) + len(flush_lines),
            )
            clear_start_row = (
                self._last_frame_start_row
                if self._has_rendered_frame and self._last_frame_start_row > 0
                else 0
            )

            if worker_mode:
                self._invalidate_layout("commit")
                token = self._terminal_writer.submit_commit(
                    clear_start_row=clear_start_row,
                    ansi=commit_ansi,
                )
                if callable(getattr(self._terminal_writer, "wait", None)):
                    self._track_pending_commit(
                        token,
                        apply_state=apply_state,
                        flush_rows=flush_rows,
                        force_requested=force_requested,
                        raw_echoes=raw_echoes,
                    )
                else:
                    apply_state()
                    term_height = shutil.get_terminal_size().lines
                    self._visible_committed_rows = min(
                        term_height,
                        self._visible_committed_rows + flush_rows,
                    )
                    self._invalidate_frame_cache()
                return token

            if clear_start_row > 0:
                self._terminal_writer.write(f"\x1b[{clear_start_row};1H")
                self._terminal_writer.write("\x1b[J")
                self._terminal_writer.flush()
            self._terminal_writer.write(commit_ansi)
            self._terminal_writer.flush()
            apply_state()
            term_height = shutil.get_terminal_size().lines
            self._visible_committed_rows = min(
                term_height,
                self._visible_committed_rows + flush_rows,
            )
            self._invalidate_frame_cache()
            return None
        except Exception:
            if worker_mode:
                if force_requested:
                    dock.request_force_flush()
                dock.restore_guidance_echoes(raw_echoes)
            raise

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
                context=self._submit_context(lock_from_runtime_state=False),
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
        context = self._submit_context(lock_from_runtime_state=not stripped.startswith("/"))
        if self._locked_submit_context is None and not stripped.startswith("/"):
            self._locked_submit_context = context
        self._queue.put_nowait(_SubmitQueueItem(
            submit_text,
            restore_text=draft_text,
            paste_entries=paste_entries,
            context=context,
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
        if loop_active:
            self._stop_active_loop_from_interrupt()
            return
        if not self._is_input_empty():
            self._clear_input()
            self._reset_ctrl_c()
            self._notice = "Input cleared. Press Ctrl-C twice on empty input to exit."
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

    def _stop_active_loop_from_interrupt(self) -> None:
        if self._active_text_prompt is not None:
            self._cancel_text_prompt()
        if self._active_choice is not None:
            self._finish_choice(None)
        if self._current_submit_task is not None and not self._current_submit_task.done():
            self._submit_cancel_requested = True
            self._current_submit_task.cancel()
        if not self._is_input_empty():
            self._clear_input()
        command = "/loop stop"
        self._quiet_commands.append(command)
        self._queue.put_nowait(_SubmitQueueItem(
            command,
            restore_text="",
            paste_entries=[],
            context=self._submit_context(),
        ))
        self._reset_ctrl_c()
        self._notice = "Stopping loop..."
        self.invalidate()

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
                context = self._submit_context(
                    thread_id=thread_id,
                    lock_from_runtime_state=not submit_text.strip().startswith("/"),
                )
            if self._locked_submit_context is None and not submit_text.strip().startswith("/"):
                self._locked_submit_context = context

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
                from voidx.presentation.output.events import ErrorAppended, ui_events, via_events

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
                clear_guidance_preview = getattr(dock, "clear_guidance_preview", None)
                if callable(clear_guidance_preview):
                    clear_guidance_preview()
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
