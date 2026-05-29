"""prompt_toolkit based full-screen UI for voidx."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app_or_none
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import AnyFormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea

from voidx.ui.app_parts.commands import SlashCommandCompleter
from voidx.ui.app_parts.controls import TranscriptControl, TranscriptScrollbarMargin
from voidx.ui.app_parts.formatting import (
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
from voidx.ui.app_parts.file_picker import attachment_token_text
from voidx.ui.app_parts.clipboard_image import (
    ClipboardImageResult,
    paste_clipboard_image as paste_clipboard_image_from_system,
)
from voidx.ui.app_parts.rendering import PromptToolkitRenderMixin
from voidx.ui.dock import dock
from voidx.ui.dock_parts.formatting import _ansi_line, _strip_ansi_trailing_space
from voidx.ui.events import ErrorAppended, ui_events


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
    mcp_servers: Callable[[], list[McpServerStatus]] = field(default_factory=lambda: lambda: [])
    mcp_config_path: str = ""


class PromptToolkitTui(PromptToolkitRenderMixin):
    """Scrollable transcript with a fixed bottom input."""

    def __init__(self, status: UiStatus, commands: list[tuple[str, str]]) -> None:
        self.status = status
        self.commands = commands
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
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
        self._scroll_offset = 0
        self._busy = False
        self._last_error = ""
        self._notice = ""
        self._ctrl_c_armed = False
        self._ctrl_c_deadline = 0.0
        self._exit_requested = False
        self._visible_body_lines: list[str] = []
        self._visible_body_node_ids: list[str | None] = []
        self._last_body_click: Point | None = None
        self._command_selected = 0
        self._command_panel_suppressed_text = ""
        self._attachment_selected = 0
        self._attachment_panel_suppressed_text = ""
        self._command_output_title = ""
        self._command_output_lines: list[str] = []
        self._command_output_visible = False

        self.input = TextArea(
            height=Dimension(min=2, preferred=2, max=2),
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
        command_output_wide_mode = Condition(lambda: self._command_output_wide_active())
        command_output_bottom_mode = Condition(
            lambda: self._command_output_active() and not self._command_output_wide_active()
        )

        @kb.add("enter", filter=choice_mode)
        def _(event) -> None:
            val = self._active_choice[self._choice_selected][1]
            self._choice_queue.put_nowait(val)
            self._active_choice = None
            event.app.invalidate()

        @kb.add("escape", filter=choice_mode)
        def _(event) -> None:
            self._choice_queue.put_nowait(None)
            self._active_choice = None
            event.app.invalidate()

        @kb.add("up", filter=choice_mode)
        def _(event) -> None:
            self._choice_selected = (self._choice_selected - 1) % len(self._active_choice)
            event.app.invalidate()

        @kb.add("down", filter=choice_mode)
        def _(event) -> None:
            self._choice_selected = (self._choice_selected + 1) % len(self._active_choice)
            event.app.invalidate()

        @kb.add("<any>", filter=choice_mode)
        def _(event) -> None:
            char = event.key_sequence[0].data
            quick_keys = {v: v for _, v, _ in self._active_choice if len(v) == 1}
            if char in quick_keys:
                self._choice_queue.put_nowait(quick_keys[char])
                self._active_choice = None
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
            self._move_command_selection(-1)
            event.app.invalidate()

        @kb.add("up", filter=attachment_mode)
        def _(event) -> None:
            self._move_attachment_selection(-1)
            event.app.invalidate()

        @kb.add("down", filter=command_mode)
        def _(event) -> None:
            self._move_command_selection(1)
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

        @kb.add("enter", filter=~choice_mode & ~text_mode & ~command_mode & ~attachment_mode)
        def _(event) -> None:
            self._submit_input()
            event.app.invalidate()

        @kb.add("c-j")
        def _(event) -> None:
            self._reset_ctrl_c()
            self.input.buffer.insert_text("\n")
            event.app.invalidate()

        @kb.add("c-c")
        def _(event) -> None:
            self._handle_ctrl_c()
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

        choice_panel = ConditionalContainer(
            Window(
                FormattedTextControl(self._render_choice_panel),
                height=Dimension(min=5, preferred=14, max=16),
                dont_extend_height=True,
                style="class:permission",
            ),
            filter=choice_mode,
        )
        command_panel = ConditionalContainer(
            Window(
                FormattedTextControl(self._render_command_panel),
                height=Dimension(min=1, preferred=8, max=12),
                dont_extend_height=True,
                style="class:command",
            ),
            filter=command_mode,
        )
        attachment_panel = ConditionalContainer(
            Window(
                FormattedTextControl(self._render_attachment_panel),
                height=Dimension(min=1, preferred=8, max=12),
                dont_extend_height=True,
                style="class:command",
            ),
            filter=attachment_mode,
        )
        bottom_command_output = ConditionalContainer(
            Window(
                FormattedTextControl(self._render_command_output_panel),
                height=Dimension(min=3, preferred=8, max=12),
                dont_extend_height=True,
                wrap_lines=True,
                style="class:command.output",
            ),
            filter=command_output_bottom_mode,
        )
        right_command_output = ConditionalContainer(
            VSplit(
                [
                    Window(char="│", width=1, style="class:rule"),
                    Window(
                        FormattedTextControl(self._render_command_output_panel),
                        width=Dimension(min=36, preferred=44, max=50),
                        wrap_lines=True,
                        dont_extend_width=True,
                        style="class:command.output",
                    ),
                ]
            ),
            filter=command_output_wide_mode,
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
                Window(char="─", height=1, style="class:rule"),
                choice_panel,
                self.input,
                command_panel,
                attachment_panel,
                bottom_command_output,
                Window(FormattedTextControl(self._render_footer), height=1, style="class:hints"),
                Window(char="─", height=1, style="class:rule"),
            ]
        )
        root = VSplit([left, right_command_output])

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
            self.invalidate()
            try:
                keep_running = await on_submit(item)
            except Exception as exc:
                self._last_error = str(exc)
                if dock.active and ui_events.is_running:
                    ui_events.emit_nowait(ErrorAppended(message=str(exc)))
                else:
                    dock.append_error(str(exc))
                keep_running = True
            finally:
                self._busy = False
                self.invalidate()
            if not keep_running:
                self._exit_requested = True
                self._exit_app()
                return

    def _on_input_changed(self, _) -> None:
        if self.input.text:
            self._reset_ctrl_c()
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
                self._toggle_body_node_at(mouse_event.position.y)
            self.invalidate()
            return None
        if event_type == MouseEventType.MOUSE_MOVE:
            return None
        return None

    def _handle_ctrl_c(self) -> None:
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

    async def ask_choice(
        self,
        prompt: str,
        choices: list[tuple[str, str, str]],
        details: list[dict[str, Any]] | None = None,
    ) -> str | None:
        self._active_choice = choices
        self._choice_prompt = prompt
        self._choice_selected = 0
        self._choice_details = details or []
        self.invalidate()
        try:
            return await self._choice_queue.get()
        finally:
            self._choice_details = []

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

    def begin_command_output(self, title: str) -> None:
        self._command_output_title = title
        self._command_output_lines = []
        self._command_output_visible = True
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
        self.invalidate()

    def hide_command_output(self) -> None:
        self._command_output_visible = False
        self.invalidate()

    def clear_command_output(self) -> None:
        self._command_output_title = ""
        self._command_output_lines = []
        self._command_output_visible = False
        self.invalidate()

    def command_output_width(self) -> int:
        if self._command_output_wide_possible():
            return self._command_output_side_width()
        return max(self._main_width() - 1, 20)

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
        "dim": "#D8DEE9",
        "command": "bg:#2E3440 #D8DEE9",
        "command.divider": "#B7C1FF bg:#2E3440",
        "command.title": "bold #B7C1FF bg:#2E3440",
        "command.group": "bold #ECEFF4 bg:#2E3440",
        "command.name": "#ECEFF4 bg:#2E3440",
        "command.selected": "bold #B7C1FF bg:#2E3440",
        "command.marker": "bold #B7C1FF bg:#2E3440",
        "command.dim": "#9AA1AD bg:#2E3440",
        "command.ok": "#5FD27A bg:#2E3440",
        "command.error": "#BF616A bg:#2E3440",
        "command.output": "bg:#2E3440 #D8DEE9",
        "permission": "bg:#2E3440 #D8DEE9",
        "permission.border": "#5E81AC bg:#2E3440",
        "permission.title": "bold #EBCB8B bg:#2E3440",
        "permission.prompt": "bold #ECEFF4 bg:#2E3440",
        "permission.tool": "bold #8FBCBB bg:#2E3440",
        "permission.dim": "#A7B0BE bg:#2E3440",
        "permission.marker": "bold #EBCB8B bg:#2E3440",
        "permission.choice": "#D8DEE9 bg:#2E3440",
        "permission.choice.selected": "bold #2E3440 bg:#EBCB8B",
        "permission.key": "#88C0D0 bg:#2E3440",
        "scrollbar.background": "bg:#3B4252",
        "scrollbar.button": "bg:#D8DEE9",
    }
)
