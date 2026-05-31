"""prompt_toolkit based full-screen UI for voidx."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app_or_none
from prompt_toolkit.clipboard import ClipboardData
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import AnyFormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.containers import ConditionalContainer, Float, FloatContainer
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.selection import SelectionType
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea

from voidx.ui.app_components.commands import SlashCommandCompleter
from voidx.ui.app_components.controls import TranscriptControl, TranscriptScrollbarMargin
from voidx.ui.app_components.formatting import (
    _args_preview,
    _clip,
    _continuation_prefix,
    _friendly_choice_label,
    _get_ansi_console,
    _lines_to_formatted_text,
    _mcp_status_label,
    _permission_target,
    _rich_to_ansi,
    _visible_text,
)
from voidx.ui.app_components.file_picker import attachment_token_text
from voidx.ui.app_components.clipboard_image import (
    ClipboardImageResult,
    paste_clipboard_image as paste_clipboard_image_from_system,
)
from voidx.ui.app_components.rendering import PromptToolkitRenderMixin
from voidx.ui.dock import dock
from voidx.ui.dock_components.formatting import _ansi_line, _strip_ansi_trailing_space
from voidx.ui.events import ErrorAppended, ui_events
from voidx.ui.session_changes import session_tracker
from voidx.llm.usage import UsageStats


SubmitHandler = Callable[[str], Awaitable[bool]]


@dataclass
class McpServerStatus:
    name: str
    status: str = "configured"
    tool_count: int = 0
    source: str = "Project MCPs"


@dataclass
class UiStatus:
    provider: str
    model: str
    workspace: str
    session_title: str
    context_limit: int
    debug: Callable[[], bool]
    plan_mode: Callable[[], bool]
    interaction_mode: Callable[[], str] = field(default_factory=lambda: lambda: "auto")
    goal_label: Callable[[], str] = field(default_factory=lambda: lambda: "")
    goal_phase: Callable[[], str] = field(default_factory=lambda: lambda: "clarify")
    goal_status: Callable[[], str] = field(default_factory=lambda: lambda: "idle")
    goal_turn_count: Callable[[], int] = field(default_factory=lambda: lambda: 0)
    goal_awaiting_approval: Callable[[], bool] = field(default_factory=lambda: lambda: False)
    reasoning_effort: str = "xhigh"
    permission_label: Callable[[], str] = field(default_factory=lambda: lambda: "default")
    sandbox_label: Callable[[], str] = field(default_factory=lambda: lambda: "w-write")
    approval_label: Callable[[], str] = field(default_factory=lambda: lambda: "on-fail")
    approval_reviewer_label: Callable[[], str] = field(default_factory=lambda: lambda: "user")
    usage_stats: UsageStats = field(default_factory=UsageStats)
    mcp_servers: Callable[[], list[McpServerStatus]] = field(default_factory=lambda: lambda: [])
    mcp_config_path: str = ""
    code_ide: Callable[[], str] = field(default_factory=lambda: lambda: "trae")


class PromptToolkitTui(PromptToolkitRenderMixin):
    """Scrollable transcript with a fixed bottom input."""

    COMMAND_OUTPUT_TTL_SECONDS = 5.0

    def __init__(self, status: UiStatus, commands: list[tuple[str, str]]) -> None:
        self.status = status
        self.commands = commands
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._quiet_commands: list[str] = []
        self._choice_queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._text_queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._active_choice: list[tuple[str, str, str]] | None = None
        self._active_text_prompt: str | None = None
        self._active_text_default = ""
        self._active_text_secret = False
        self._saved_input_text = ""
        self._saved_input_cursor = 0
        self._choice_prompt: str = ""
        self._choice_selected: int = 0
        self._choice_details: list[dict[str, Any]] = []
        self._choice_anchor = ""
        self._choice_current_value: str = ""
        self._footer_anchor_positions: dict[str, int] = {}
        self._scroll_offset = 0
        self._busy = False
        self._last_error = ""
        self._notice = ""
        self._ctrl_c_armed = False
        self._ctrl_c_deadline = 0.0
        self._exit_requested = False
        self._visible_body_lines: list[str] = []
        self._visible_body_node_ids: list[str | None] = []
        self._visible_row_to_node: dict[int, str | None] = {}
        self._last_body_click: Point | None = None
        self._body_mouse_down: Point | None = None
        self._command_selected = 0
        self._command_panel_suppressed_text = ""
        self._attachment_selected = 0
        self._attachment_panel_suppressed_text = ""
        self._command_output_title = ""
        self._command_output_lines: list[str] = []
        self._command_output_visible = False
        self._command_output_clear_handle: asyncio.TimerHandle | None = None
        self._input_history: list[str] = []
        self._input_history_index: int | None = None
        self._input_history_draft = ""
        self._loading_input_history = False
        self._review_active = False
        self._current_submit_task: asyncio.Task[bool] | None = None
        self._current_submitted_text = ""
        self._submit_cancel_requested = False

        self.input = TextArea(
            height=Dimension(min=3, preferred=3, max=3),
            multiline=True,
            password=Condition(lambda: self._active_text_secret),
            wrap_lines=True,
            prompt=self._input_prompt,
            style="class:input",
        )
        self.input.buffer.on_text_changed += self._on_input_changed
        self.input.control.mouse_handler = self._ignore_input_mouse
        self.body_control = TranscriptControl(self)

        kb = KeyBindings()

        choice_mode = Condition(lambda: self._active_choice is not None)
        text_mode = Condition(lambda: self._active_text_prompt is not None)
        command_mode = Condition(lambda: self._command_panel_active())
        attachment_mode = Condition(lambda: self._attachment_panel_active())
        command_output_mode = Condition(lambda: self._command_output_active())
        compact_choice_mode = Condition(
            lambda: self._active_choice is not None
        )
        has_choice_details = Condition(lambda: bool(self._choice_details))
        footer_choice_mode = compact_choice_mode & ~has_choice_details
        permission_choice_mode = compact_choice_mode & has_choice_details
        has_changes = Condition(lambda: session_tracker.has_changes)
        review_mode = Condition(lambda: self._review_active)

        @kb.add("escape", filter=choice_mode)
        def _(event) -> None:
            self._finish_choice(None)
            event.app.invalidate()

        @kb.add("enter", filter=choice_mode)
        def _(event) -> None:
            self._submit_choice_selection()
            event.app.invalidate()

        @kb.add("up", filter=choice_mode)
        def _(event) -> None:
            self._move_choice_selection(-1)
            event.app.invalidate()

        @kb.add("down", filter=choice_mode)
        def _(event) -> None:
            self._move_choice_selection(1)
            event.app.invalidate()

        @kb.add("<any>", filter=choice_mode)
        def _(event) -> None:
            char = event.key_sequence[0].data
            quick_keys = {value: value for _, value, _ in self._active_choice or [] if len(value) == 1}
            if char in quick_keys:
                self._finish_choice(quick_keys[char])
                event.app.invalidate()

        @kb.add("enter", filter=text_mode)
        def _(event) -> None:
            self._submit_text_prompt()
            event.app.invalidate()

        @kb.add("escape", filter=text_mode)
        def _(event) -> None:
            self._cancel_text_prompt()
            event.app.invalidate()

        @kb.add("enter", filter=command_mode)
        def _(event) -> None:
            if self._accept_command_panel_selection():
                event.app.invalidate()
                return
            self._submit_input()
            event.app.invalidate()

        @kb.add("enter", filter=attachment_mode)
        def _(event) -> None:
            if self._accept_attachment_panel_selection():
                event.app.invalidate()
                return
            self._submit_input()
            event.app.invalidate()

        @kb.add("up", filter=command_mode)
        def _(event) -> None:
            self._move_command_selection_visual(-1)
            event.app.invalidate()

        @kb.add("up", filter=attachment_mode)
        def _(event) -> None:
            self._move_attachment_selection(-1)
            event.app.invalidate()

        @kb.add("down", filter=command_mode)
        def _(event) -> None:
            self._move_command_selection_visual(1)
            event.app.invalidate()

        @kb.add("down", filter=attachment_mode)
        def _(event) -> None:
            self._move_attachment_selection(1)
            event.app.invalidate()

        @kb.add("escape", filter=command_mode)
        def _(event) -> None:
            self._command_panel_suppressed_text = self.input.text
            event.app.invalidate()

        @kb.add("escape", filter=attachment_mode)
        def _(event) -> None:
            self._attachment_panel_suppressed_text = self.input.text
            event.app.invalidate()

        @kb.add("escape", filter=command_output_mode & ~choice_mode & ~command_mode & ~attachment_mode)
        def _(event) -> None:
            self.hide_command_output()
            event.app.invalidate()

        @kb.add("escape", filter=review_mode)
        def _(event) -> None:
            self._review_active = False
            event.app.invalidate()

        @kb.add("enter", filter=~choice_mode & ~text_mode & ~command_mode & ~attachment_mode & ~review_mode)
        def _(event) -> None:
            self._submit_input()
            event.app.invalidate()

        @kb.add("escape", "enter", filter=~choice_mode & ~text_mode)
        def _(event) -> None:
            self._insert_input_newline()
            event.app.invalidate()

        @kb.add("c-j")
        def _(event) -> None:
            self._insert_input_newline()
            event.app.invalidate()

        @kb.add("s-left", filter=~choice_mode)
        def _(event) -> None:
            self._extend_input_selection(-1)
            event.app.invalidate()

        @kb.add("s-right", filter=~choice_mode)
        def _(event) -> None:
            self._extend_input_selection(1)
            event.app.invalidate()

        @kb.add("up", filter=~choice_mode & ~text_mode & ~command_mode & ~attachment_mode)
        def _(event) -> None:
            self._previous_input_history()
            event.app.invalidate()

        @kb.add("down", filter=~choice_mode & ~text_mode & ~command_mode & ~attachment_mode)
        def _(event) -> None:
            self._next_input_history()
            event.app.invalidate()

        @kb.add("c-c")
        def _(event) -> None:
            if self._copy_input_selection(event):
                event.app.invalidate()
                return
            self._handle_ctrl_c()
            event.app.invalidate()

        @kb.add("escape", "c", filter=~choice_mode)
        def _(event) -> None:
            self._copy_input_selection(event)
            event.app.invalidate()

        @kb.add("c-d")
        def _(event) -> None:
            if not self.input.text:
                self._request_exit()

        @kb.add("c-v", filter=~choice_mode)
        def _(event) -> None:
            result = self.paste_clipboard_image(quiet_no_image=True)
            if not result.ok:
                self.input.buffer.paste_clipboard_data(event.app.clipboard.get_data())
            event.app.invalidate()

        compact_choice_overlay = ConditionalContainer(
            Window(
                FormattedTextControl(self._render_compact_choice_panel),
                width=self._choice_float_width,
                height=lambda: self._choice_panel_height(),
                dont_extend_width=True,
                dont_extend_height=True,
                style="class:choice.pad",
            ),
            filter=footer_choice_mode,
        )
        permission_choice_overlay = ConditionalContainer(
            Window(
                FormattedTextControl(self._render_compact_choice_panel),
                width=lambda: self._choice_menu_width(),
                height=lambda: self._choice_panel_height(),
                dont_extend_width=True,
                dont_extend_height=True,
                style="class:choice.pad",
            ),
            filter=permission_choice_mode,
        )
        command_panel = ConditionalContainer(
            Window(
                FormattedTextControl(self._render_command_panel),
                height=lambda: self._command_panel_height(),
                dont_extend_height=True,
                style="class:command",
            ),
            filter=command_mode,
        )
        attachment_panel = ConditionalContainer(
            Window(
                FormattedTextControl(self._render_attachment_panel),
                height=lambda: self._command_panel_height(),
                dont_extend_height=True,
                style="class:command",
            ),
            filter=attachment_mode,
        )
        changes_bar = ConditionalContainer(
            Window(
                FormattedTextControl(self._render_changes_bar),
                height=1,
                style="class:body",
            ),
            filter=has_changes,
        )
        review_panel = ConditionalContainer(
            Window(
                FormattedTextControl(self._render_review_panel),
                height=lambda: self._review_panel_height(),
                dont_extend_height=True,
                style="class:body",
            ),
            filter=review_mode,
        )
        command_output_overlay = ConditionalContainer(
            Window(
                FormattedTextControl(self._render_command_output_panel),
                width=self._command_output_float_width,
                height=self._command_output_float_height,
                dont_extend_width=True,
                dont_extend_height=True,
                wrap_lines=True,
                style="class:command-output",
            ),
            filter=command_output_mode,
        )
        bottom_bar = VSplit(
            [
                HSplit(
                    [
                        self.input,
                        Window(FormattedTextControl(self._render_footer), height=1, style="class:hints"),
                    ],
                    width=self._input_panel_width,
                    height=self._bottom_bar_height(),
                    style="class:body",
                ),
                Window(char="│", width=1, style="class:rule"),
                Window(
                    FormattedTextControl(self._render_detail_status_panel),
                    width=self._detail_status_width,
                    height=self._bottom_bar_height(),
                    style="class:status",
                ),
            ],
            height=self._bottom_bar_height(),
        )

        left = HSplit(
            [
                Window(
                    self.body_control,
                    right_margins=[TranscriptScrollbarMargin(self)],
                    wrap_lines=True,
                    dont_extend_height=False,
                    style="class:body",
                    get_line_prefix=self._body_line_prefix,
                ),
                Window(height=self._transcript_bottom_gap_height, style="class:body"),
                Window(char="─", height=1, style="class:rule"),
                command_panel,
                attachment_panel,
                review_panel,
                changes_bar,
                bottom_bar,
                Window(char="─", height=1, style="class:rule"),
            ]
        )
        self._compact_choice_float = Float(
            content=compact_choice_overlay,
            left=0,
            bottom=2,
            width=self._choice_float_width,
            height=self._choice_panel_height,
            transparent=True,
            z_index=20,
        )
        self._permission_choice_float = Float(
            content=permission_choice_overlay,
            left=0,
            bottom=self.BOTTOM_BAR_HEIGHT + 1,
            width=lambda: self._choice_menu_width(),
            height=self._choice_panel_height,
            transparent=True,
            z_index=21,
        )
        root = FloatContainer(
            content=left,
            floats=[
                self._compact_choice_float,
                self._permission_choice_float,
                Float(
                    content=command_output_overlay,
                    top=1,
                    right=2,
                    width=self._command_output_float_width,
                    height=self._command_output_float_height,
                    transparent=True,
                    z_index=10,
                )
            ],
        )

        self.app: Application = Application(
            layout=Layout(root, focused_element=self.input),
            key_bindings=kb,
            full_screen=True,
            mouse_support=True,
            refresh_interval=None,
            style=_STYLE,
        )

    async def run(self, on_submit: SubmitHandler) -> None:
        dock.set_refresh_callback(self.invalidate)
        dock.set_width_provider(self._main_width)
        consumer = asyncio.create_task(self._consume(on_submit))
        try:
            await self.app.run_async()
        finally:
            dock.set_refresh_callback(None)
            dock.set_width_provider(None)
            self._cancel_command_output_clear()
            consumer.cancel()
            try:
                await consumer
            except asyncio.CancelledError:
                pass

    def invalidate(self) -> None:
        app = get_app_or_none()
        if app is not None:
            app.invalidate()
        else:
            self.app.invalidate()

    async def _consume(self, on_submit: SubmitHandler) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                if not self._exit_requested:
                    self._exit_requested = True
                    self._exit_app()
                return
            self._reset_ctrl_c()
            self._busy = True
            self._current_submitted_text = item
            self._submit_cancel_requested = False
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
                if dock.active and ui_events.is_running:
                    ui_events.emit_nowait(ErrorAppended(message=str(exc)))
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

    def _on_input_changed(self, _) -> None:
        if self.input.text:
            self._reset_ctrl_c()
        if not self._loading_input_history:
            self._input_history_index = None
            self._input_history_draft = ""
        if self.input.text != self._command_panel_suppressed_text:
            self._command_panel_suppressed_text = ""
        if self.input.text != self._attachment_panel_suppressed_text:
            self._attachment_panel_suppressed_text = ""
        self._clamp_command_selection()
        self._clamp_attachment_selection()

    def _ignore_input_mouse(self, mouse_event: MouseEvent) -> None:
        return None

    def _input_prompt(self) -> AnyFormattedText:
        if self._active_text_prompt is not None:
            return [("class:input.prompt", f"{self._active_text_prompt}: ")]
        return [("class:input.prompt", "❯ ")]

    def _handle_body_mouse(self, mouse_event: MouseEvent) -> None:
        event_type = mouse_event.event_type
        if event_type == MouseEventType.SCROLL_UP:
            self._scroll_by(3)
            self.invalidate()
            return None
        if event_type == MouseEventType.SCROLL_DOWN:
            self._scroll_by(-3)
            self.invalidate()
            return None
        if event_type in (MouseEventType.MOUSE_DOWN, MouseEventType.MOUSE_UP):
            self._last_body_click = mouse_event.position
            if event_type == MouseEventType.MOUSE_DOWN:
                self._body_mouse_down = mouse_event.position
            else:
                down = self._body_mouse_down
                self._body_mouse_down = None
                if down is None or down.y == mouse_event.position.y:
                    self._toggle_body_node_at(mouse_event.position.y)
            self.invalidate()
            return None
        if event_type == MouseEventType.MOUSE_MOVE:
            return None
        return None

    def _handle_ctrl_c(self) -> None:
        if self._busy and self._current_submit_task is not None:
            if not self._current_submit_task.done():
                self._submit_cancel_requested = True
                self._current_submit_task.cancel()
            self._restore_interrupted_input()
            self._reset_ctrl_c()
            self._notice = "Interrupted. Restored last message for editing."
            return

        if self.input.text:
            self.input.text = ""
            self._reset_ctrl_c()
            self._notice = "Input cleared. Press Ctrl-C twice on empty input to exit."
            return

        now = time.monotonic()
        if not self._ctrl_c_armed or now > self._ctrl_c_deadline:
            self._ctrl_c_armed = True
            self._ctrl_c_deadline = now + 3.0
            self._notice = "Press Ctrl-C again to exit"
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
        if self.input.buffer.selection_state is not None:
            self.input.buffer.exit_selection()
        self.input.text = text
        self.input.buffer.cursor_position = len(text)
        self._command_panel_suppressed_text = ""
        self._attachment_panel_suppressed_text = ""

    def _request_exit(self) -> None:
        if self._exit_requested:
            return
        self._exit_requested = True
        if self.app.is_running:
            self._exit_app()
        else:
            self._queue.put_nowait(None)

    def _exit_app(self) -> None:
        if not self.app.is_running:
            return
        try:
            self.app.exit()
        except Exception as exc:
            if "Return value already set" not in str(exc):
                raise

    def _insert_input_newline(self) -> None:
        self._reset_ctrl_c()
        self.input.buffer.insert_text("\n", fire_event=False)

    def _extend_input_selection(self, amount: int) -> None:
        if amount == 0:
            return
        buffer = self.input.buffer
        if buffer.selection_state is None:
            buffer.start_selection(selection_type=SelectionType.CHARACTERS)
        if amount < 0:
            buffer.cursor_left(count=abs(amount))
        else:
            buffer.cursor_right(count=amount)
        self._reset_ctrl_c()

    def _input_selection_text(self) -> str:
        buffer = self.input.buffer
        selection = buffer.selection_state
        if selection is None:
            return ""
        start = min(selection.original_cursor_position, buffer.cursor_position)
        end = max(selection.original_cursor_position, buffer.cursor_position)
        return buffer.text[start:end]

    def _copy_input_selection(self, event: Any | None = None) -> bool:
        text = self._input_selection_text()
        if not text:
            return False
        data = ClipboardData(text)

        app = getattr(event, "app", None)
        clipboard = getattr(app, "clipboard", None)
        if clipboard is not None:
            try:
                clipboard.set_data(data)
            except AttributeError:
                clipboard.set_text(data.text)
        self._copy_text_to_system_clipboard(data.text)
        self._reset_ctrl_c()
        self._notice = "Copied selection"
        return True

    def _copy_text_to_system_clipboard(self, text: str) -> None:
        if not text or sys.platform != "darwin":
            return
        try:
            subprocess.run(["pbcopy"], input=text, text=True, timeout=1, check=False)
        except Exception:
            return

    def _record_input_history(self, text: str) -> None:
        if not text.strip():
            return
        if not self._input_history or self._input_history[-1] != text:
            self._input_history.append(text)
            if len(self._input_history) > 200:
                self._input_history = self._input_history[-200:]
        self._input_history_index = None
        self._input_history_draft = ""

    def _set_input_from_history(self, text: str) -> None:
        buffer = self.input.buffer
        if buffer.selection_state is not None:
            buffer.exit_selection()
        self._loading_input_history = True
        try:
            self.input.text = text
            self.input.buffer.cursor_position = len(text)
        finally:
            self._loading_input_history = False
        self._review_active = False
        self._reset_ctrl_c()

    def _previous_input_history(self) -> None:
        if not self._input_history:
            return
        if self._input_history_index is None:
            self._input_history_draft = self.input.text
            self._input_history_index = len(self._input_history) - 1
        else:
            self._input_history_index = max(0, self._input_history_index - 1)
        self._set_input_from_history(self._input_history[self._input_history_index])

    def _next_input_history(self) -> None:
        if self._input_history_index is None:
            return
        if self._input_history_index < len(self._input_history) - 1:
            self._input_history_index += 1
            self._set_input_from_history(self._input_history[self._input_history_index])
            return
        draft = self._input_history_draft
        self._input_history_index = None
        self._input_history_draft = ""
        self._set_input_from_history(draft)

    def _choice_initial_index(self, choices: list[tuple[str, str, str]]) -> int:
        if not self._choice_current_value:
            return 0
        cv = self._choice_current_value
        for i, (label, value, _desc) in enumerate(choices):
            if cv == value or cv == label:
                return i
        return 0

    async def ask_choice(
        self,
        prompt: str,
        choices: list[tuple[str, str, str]],
        details: list[dict[str, Any]] | None = None,
    ) -> str | None:
        self._active_choice = choices
        self._choice_prompt = prompt
        self._choice_selected = self._choice_initial_index(choices)
        self._choice_details = details or []
        if not self._choice_anchor:
            self._choice_anchor = self._choice_anchor_for_prompt(prompt)
        self.invalidate()
        try:
            return await self._choice_queue.get()
        finally:
            self._active_choice = None
            self._choice_details = []
            self._choice_anchor = ""

    def _finish_choice(self, value: str | None) -> None:
        if self._active_choice is None:
            return
        self._choice_queue.put_nowait(value)
        self._active_choice = None
        self._choice_details = []
        self._choice_anchor = ""

    def _move_choice_selection(self, amount: int) -> None:
        choices = self._active_choice or []
        if not choices:
            return
        self._choice_selected = (self._choice_selected + amount) % len(choices)

    def _submit_choice_selection(self) -> None:
        choices = self._active_choice or []
        if not choices:
            return
        index = max(0, min(self._choice_selected, len(choices) - 1))
        self._finish_choice(choices[index][1])

    async def ask_text(self, prompt: str, default: str = "", secret: bool = False) -> str | None:
        if self._active_text_prompt is not None:
            raise RuntimeError("Text prompt is already active")
        self._saved_input_text = self.input.text
        self._saved_input_cursor = self.input.buffer.cursor_position
        self._active_text_prompt = prompt
        self._active_text_default = default
        self._active_text_secret = secret
        self.input.text = ""
        self.input.buffer.cursor_position = 0
        self._command_panel_suppressed_text = ""
        self._attachment_panel_suppressed_text = ""
        self.invalidate()
        try:
            result = await self._text_queue.get()
            if result is None:
                return None
            return result if result else default
        finally:
            if self._active_text_prompt is not None:
                self._restore_text_prompt()

    def set_notice(self, text: str) -> None:
        self._notice = text
        self.invalidate()

    def show_transient_output(self, text: str, title: str = "") -> None:
        self.begin_command_output(title)
        self.append_command_output(text)

    def begin_command_output(self, title: str) -> None:
        self._command_output_title = title
        self._command_output_lines = []
        self._command_output_visible = False
        self._cancel_command_output_clear()
        self.invalidate()

    def append_command_output(self, text: str) -> None:
        if not text.strip():
            return
        for line in text.rstrip("\n").splitlines():
            cleaned = _strip_ansi_trailing_space(line)
            self._command_output_lines.append(_ansi_line(cleaned))
        if len(self._command_output_lines) > 500:
            self._command_output_lines = self._command_output_lines[-500:]
        self._command_output_visible = True
        self._schedule_command_output_clear()
        self.invalidate()

    def hide_command_output(self) -> None:
        self._command_output_visible = False
        self._cancel_command_output_clear()
        self.invalidate()

    def clear_command_output(self) -> None:
        self._command_output_title = ""
        self._command_output_lines = []
        self._command_output_visible = False
        self._cancel_command_output_clear()
        self.invalidate()

    def _schedule_command_output_clear(self) -> None:
        self._cancel_command_output_clear()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._command_output_clear_handle = loop.call_later(
            self.COMMAND_OUTPUT_TTL_SECONDS,
            self.clear_command_output,
        )

    def _cancel_command_output_clear(self) -> None:
        handle = self._command_output_clear_handle
        self._command_output_clear_handle = None
        if handle is not None and not handle.cancelled():
            handle.cancel()

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

    def command_output_width(self) -> int:
        return self._command_output_float_width()

    def _submit_text_prompt(self) -> None:
        value = self.input.text
        self._text_queue.put_nowait(value)
        self._restore_text_prompt()

    def _cancel_text_prompt(self) -> None:
        self._text_queue.put_nowait(None)
        self._restore_text_prompt()

    def _restore_text_prompt(self) -> None:
        saved_text = self._saved_input_text
        saved_cursor = max(0, min(self._saved_input_cursor, len(saved_text)))
        self._active_text_prompt = None
        self._active_text_default = ""
        self._active_text_secret = False
        self._saved_input_text = ""
        self._saved_input_cursor = 0
        self.input.text = saved_text
        self.input.buffer.cursor_position = saved_cursor
        self.invalidate()

    def _submit_input(self) -> None:
        text = self.input.text
        stripped = text.strip()
        if stripped == "/paste":
            self._record_input_history(text)
            self.input.text = ""
            self._scroll_offset = 0
            self._command_panel_suppressed_text = ""
            self._attachment_panel_suppressed_text = ""
            self._reset_ctrl_c()
            self.paste_clipboard_image()
            return

        self.input.text = ""
        self._scroll_offset = 0
        self._command_panel_suppressed_text = ""
        self._attachment_panel_suppressed_text = ""
        self._reset_ctrl_c()
        if stripped and not stripped.startswith("/"):
            self.clear_command_output()
        if stripped:
            self._record_input_history(text)
            self._queue.put_nowait(text)

    def paste_clipboard_image(self, *, quiet_no_image: bool = False) -> ClipboardImageResult:
        result = paste_clipboard_image_from_system(self.status.workspace)
        if result.ok:
            self._insert_attachment_token(result.rel_path)
            self.clear_command_output()
        if result.ok or not quiet_no_image:
            self._notice = result.message
        return result

    def _insert_attachment_token(self, rel_path: str) -> None:
        token = attachment_token_text(rel_path) + " "
        text = self.input.text
        cursor = max(0, min(self.input.buffer.cursor_position, len(text)))
        prefix = " " if cursor > 0 and not text[cursor - 1].isspace() else ""
        new_text = text[:cursor] + prefix + token + text[cursor:]
        self.input.text = new_text
        self.input.buffer.cursor_position = cursor + len(prefix) + len(token)
        self._attachment_panel_suppressed_text = ""
        self._command_panel_suppressed_text = ""

    def _accept_attachment_panel_selection(self) -> bool:
        token = self._attachment_token()
        if token is None:
            return False
        matches = self._attachment_matches()
        if not matches:
            return False
        selected = matches[min(self._attachment_selected, len(matches) - 1)]
        replacement = attachment_token_text(selected.rel_path) + " "
        text = self.input.text
        new_text = text[:token.start] + replacement + text[token.end:]
        self.input.text = new_text
        new_cursor = token.start + len(replacement)
        self.input.buffer.cursor_position = new_cursor
        self._attachment_panel_suppressed_text = ""
        self._attachment_selected = 0
        return True

_STYLE = Style.from_dict(
    {
        "body": "#ECEFF4 bg:#000000",
        "rule": "#4C566A",
        "input": "#ECEFF4 bg:#000000",
        "input.prompt": "bold #ECEFF4 bg:#000000",
        "hints": "#8FBCBB",
        "hints.click": "#D8DEE9",
        "footer.permission": "#A3BE8C bg:#000000",
        "footer.model": "#88C0D0 bg:#000000",
        "footer.reasoning": "#EBCB8B bg:#000000",
        "dim": "#D8DEE9",
        "status": "#8FBCBB bg:#000000",
        "status.label": "#8FBCBB bg:#000000",
        "status.value": "#D8DEE9 bg:#000000",
        "status.dim": "#9AA1AD bg:#000000",
        "choice": "#ECEFF4 bg:#000000",
        "choice.pad": "bg:#000000",
        "choice.selected": "bold #EBCB8B bg:#000000",
        "choice.prompt": "bold #ECEFF4",
        "choice.tool": "bold #8FBCBB",
        "choice.dim": "#A7B0BE",
        "command": "bg:#000000 #D8DEE9",
        "command.divider": "#B7C1FF bg:#000000",
        "command.title": "bold #B7C1FF bg:#000000",
        "command.group": "bold #ECEFF4 bg:#000000",
        "command.name": "#ECEFF4 bg:#000000",
        "command.selected": "bold #EBCB8B bg:#000000",
        "command.marker": "bold #EBCB8B bg:#000000",
        "command.dim": "#9AA1AD bg:#000000",
        "command.ok": "#5FD27A bg:#000000",
        "command.error": "#BF616A bg:#000000",
        "command-output": "#D8DEE9 bg:#000000",
        "permission": "bg:#000000 #D8DEE9",
        "permission.border": "#5E81AC bg:#000000",
        "permission.title": "bold #EBCB8B bg:#000000",
        "permission.prompt": "bold #ECEFF4 bg:#000000",
        "permission.tool": "bold #8FBCBB bg:#000000",
        "permission.dim": "#A7B0BE bg:#000000",
        "permission.marker": "bold #EBCB8B bg:#000000",
        "permission.choice": "#D8DEE9 bg:#000000",
        "permission.choice.selected": "bold #EBCB8B",
        "permission.key": "#88C0D0 bg:#000000",
        "scrollbar.background": "bg:#000000",
        "scrollbar.button": "bg:#4C566A",
        "changes": "#D8DEE9 bg:#253340",
        "changes.label": "#D8DEE9 bg:#253340",
        "changes.dim": "#9AA1AD bg:#253340",
        "changes.added": "#A3BE8C bg:#253340",
        "changes.removed": "#BF616A bg:#253340",
        "changes.review": "bold #8FBCBB bg:#253340",
        "changes.rollback": "bold #EBCB8B bg:#253340",
        "review": "#D8DEE9 bg:#253340",
        "review.file": "bold #5E81AC bg:#253340",
        "review.added": "#A3BE8C bg:#253340",
        "review.removed": "#BF616A bg:#253340",
        "review.dim": "#9AA1AD bg:#253340",
    }
)
