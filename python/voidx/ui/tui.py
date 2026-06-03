"""Pure terminal TUI — manual ANSI rendering + raw terminal input.

Renders via Rich Console captured to string, then writes directly with
explicit cursor positioning so IME overlays appear at the right spot.
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import time

try:
    import termios
    _HAS_TERMIOS = True
except ImportError:
    termios = None  # type: ignore
    _HAS_TERMIOS = False
from pathlib import Path
from typing import Any, Awaitable, Callable

from rich.cells import cell_len
from rich.console import Console, Group
from rich.text import Text

from voidx.llm.usage import format_cache_hit_rate, format_token_count
from voidx.ui.attachment_tokens import attachment_token_text, image_attachment_token_text
from voidx.ui.clipboard_image import (
    ClipboardImageResult,
    paste_clipboard_image as paste_clipboard_image_from_system,
)
from voidx.ui.dock import active_agent_step_text, dock
from voidx.ui.dock_components.formatting import ANSI_LINE_PREFIX, _ansi_line, _text_from_line
from voidx.ui.file_picker import (
    AttachmentToken,
    FileCandidate,
    find_attachment_token,
    format_size,
    list_file_candidates,
)

SubmitHandler = Callable[[str], Awaitable[bool]]

# ── ANSI / VT escape helpers ──────────────────────────────────────────────

_DISABLE_MOUSE_REPORTING = (
    "\x1b[?9l"
    "\x1b[?1000l"
    "\x1b[?1002l"
    "\x1b[?1003l"
    "\x1b[?1005l"
    "\x1b[?1006l"
    "\x1b[?1015l"
)
_ENTER_TERMINAL_SEQUENCE = f"{_DISABLE_MOUSE_REPORTING}\x1b[?25l"
_EXIT_TERMINAL_SEQUENCE = f"{_DISABLE_MOUSE_REPORTING}\x1b[?25h"


def _ctrl(key: str) -> bytes:
    return bytes([ord(key) - 0x60])


def _is_printable(data: bytes) -> bool:
    """Single-byte printable character (including space)."""
    return len(data) == 1 and 0x20 <= data[0] <= 0x7E


def _is_utf8_continuation(b: int) -> bool:
    return 0x80 <= b <= 0xBF


def _utf8_len(first: int) -> int:
    if first < 0x80:
        return 1
    if first < 0xE0:
        return 2
    if first < 0xF0:
        return 3
    return 4


def _csi_modifier_has_shift(mod: int) -> bool:
    # xterm/CSI-u modifier values are 1 + bitmask, so Shift alone is 2.
    return mod > 1 and bool((mod - 1) & 1)


def _parse_csi_modifier(params: list[str]) -> int:
    try:
        return int(params[1]) if len(params) > 1 and params[1] else 0
    except ValueError:
        return 0


def _is_mouse_csi(final: int, params: list[str]) -> bool:
    if final not in (0x4D, 0x6D):  # M/m
        return False
    return len(params) >= 3 and (
        params[0].startswith("<") or params[0].lstrip("-").isdigit()
    )


# ── PureTui ────────────────────────────────────────────────────────────────


class PureTui:
    """Scrollable transcript with a fixed bottom input — pure Rich + raw stdin."""

    COMMAND_OUTPUT_TTL_SECONDS = 5.0

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

        # Choice prompts
        self._choice_queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._active_choice: list[tuple[str, str, str]] | None = None
        self._choice_prompt: str = ""
        self._choice_selected: int = 0
        self._choice_details: list[dict[str, Any]] = []
        self._choice_anchor: str = ""

        # Text prompts
        self._text_queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._active_text_prompt: str | None = None
        self._active_text_default: str = ""
        self._active_text_secret: bool = False
        self._saved_input_lines: list[str] = [""]
        self._saved_cursor_row: int = 0
        self._saved_cursor_col: int = 0

        # Command palette
        self._command_selected: int = 0
        self._command_panel_active: bool = False

        # File attachment palette
        self._attachment_selected: int = 0
        self._attachment_panel_suppressed_text: str = ""

        # Command output overlay
        self._command_output_title: str = ""
        self._command_output_lines: list[str] = []
        self._command_output_visible: bool = False
        self._command_output_clear_handle: asyncio.TimerHandle | None = None

        # Quiet commands
        self._quiet_commands: list[str] = []

        # Rendering
        self._running: bool = False
        self._exit_requested: bool = False
        self._last_error: str = ""
        self._notice: str = ""
        self._has_rendered_frame: bool = False
        self._cursor_to_frame_top_lines: int = 0
        self._cursor_to_frame_end_lines: int = 0
        self._last_frame_rows: int = 0

        # External request handler (web gateway stub)
        self._external_request_handler: Callable[[Any], Awaitable[Any]] | None = None

        # stdin
        self._stdin_fd: int | None = self._stdin_fileno()
        self._tty: bool = False
        self._old_termios: list | None = None

    # ── public API ───────────────────────────────────────────────────────

    async def run(self, on_submit: SubmitHandler) -> None:
        dock.set_refresh_callback(self.invalidate)
        dock.set_width_provider(lambda: self._console.width or 80)

        self._tty = self._stdin_fd is not None and os.isatty(self._stdin_fd)
        if self._tty:
            self._setup_terminal()
            sys.stdout.write(_ENTER_TERMINAL_SEQUENCE)
            sys.stdout.flush()

        consumer = asyncio.create_task(self._consume(on_submit))

        try:
            self._running = True
            self._render_frame()
            while self._running:
                if self._tty:
                    data = await self._read_input_raw()
                else:
                    data = await self._read_input_line()
                if self._process_input(data):
                    self._render_frame()
        finally:
            self._running = False
            self._restore_terminal()
            if self._tty:
                sys.stdout.write(self._move_to_frame_end_sequence())
                sys.stdout.write(_EXIT_TERMINAL_SEQUENCE)
                sys.stdout.flush()
            dock.set_refresh_callback(None)
            dock.set_width_provider(None)
            self._cancel_command_output_clear()
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

    def show_transient_output(self, text: str, title: str = "") -> None:
        self.begin_command_output(title)
        if text.strip():
            buf = io.StringIO()
            console = Console(
                file=buf,
                force_terminal=True,
                color_system="truecolor",
                width=self.command_output_width,
                _environ={},
            )
            console.print(text, end="")
            captured = buf.getvalue().rstrip("\n")
            if captured:
                self.append_command_output(captured)

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
            self._command_output_lines.append(_ansi_line(line) if "\x1b[" in line else line)
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

    def paste_clipboard_image(self, *, quiet_no_image: bool = False) -> ClipboardImageResult:
        result = paste_clipboard_image_from_system(self.status.workspace)
        if result.ok:
            stem = Path(result.rel_path).stem
            self._insert_text_token(image_attachment_token_text(stem) + " ")
            self.clear_command_output()
        if result.ok or not quiet_no_image:
            self._notice = result.message
        self.invalidate()
        return result

    @property
    def command_output_width(self) -> int:
        return (self._console.width or 80) - 20

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

    async def ask_choice(
        self,
        prompt: str,
        choices: list[tuple[str, str, str]],
        selected: int = 0,
        anchor: str = "",
        details: list[dict[str, Any]] | None = None,
    ) -> str | None:
        self._choice_prompt = prompt
        self._active_choice = choices
        self._choice_selected = max(0, min(selected, len(choices) - 1))
        self._choice_details = [self._normalize_choice_detail(item) for item in (details or [])]
        self._choice_anchor = anchor
        self.invalidate()
        try:
            return await self._choice_queue.get()
        finally:
            self._active_choice = None
            self._choice_selected = 0
            self._choice_details = []
            self._choice_anchor = ""
            self.invalidate()

    async def ask_text(
        self, prompt: str, default: str = "", secret: bool = False
    ) -> str | None:
        self._saved_input_lines = list(self._input_lines)
        self._saved_cursor_row = self._cursor_row
        self._saved_cursor_col = self._cursor_col

        self._active_text_prompt = prompt
        self._active_text_default = default
        self._active_text_secret = secret
        self._input_lines = [default]
        self._cursor_row = 0
        self._cursor_col = len(default)
        self.invalidate()
        try:
            return await self._text_queue.get()
        finally:
            self._active_text_prompt = None
            self._active_text_default = ""
            self._active_text_secret = False
            self._input_lines = list(self._saved_input_lines)
            self._cursor_row = self._saved_cursor_row
            self._cursor_col = self._saved_cursor_col
            self.invalidate()

    def invalidate(self) -> None:
        if self._running:
            self._render_frame()

    def _render_frame(self) -> None:
        """Render to terminal: capture Rich output, write with cursor control."""
        width = max((self._console.width or 80) - 1, 20)
        try:
            renderable = self._render_impl()
        except Exception:
            renderable = Group(Text("Render error", style="red"))

        # Capture Rich's ANSI output
        buf = io.StringIO()
        cap = Console(file=buf, force_terminal=True, color_system="truecolor",
                       width=width + 2, height=self._console.height)
        cap.print(renderable)
        ansi = buf.getvalue()

        if self._tty:
            sys.stdout.write(self._move_to_frame_top_sequence())
            sys.stdout.write("\x1b[J")
            sys.stdout.write(ansi)
            self._position_input_cursor(_rendered_row_count(ansi))
            self._has_rendered_frame = True
            sys.stdout.flush()
        else:
            # Non-TTY: strip ANSI for clean output
            import re
            clean = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', ansi)
            sys.stdout.write(clean)
            sys.stdout.flush()

    def _move_to_frame_top_sequence(self) -> str:
        if not self._has_rendered_frame:
            return ""
        sequence = "\r"
        if self._cursor_to_frame_end_lines > 0:
            sequence += f"\x1b[{self._cursor_to_frame_end_lines}B\r"
        if self._last_frame_rows > 0:
            sequence += f"\x1b[{self._last_frame_rows}A\r"
        return sequence

    def _move_to_frame_end_sequence(self) -> str:
        if not self._has_rendered_frame:
            return ""
        if self._cursor_to_frame_end_lines <= 0:
            return "\r"
        return f"\r\x1b[{self._cursor_to_frame_end_lines}B\r"

    def _position_input_cursor(self, frame_rows: int | None = None) -> None:
        """Move terminal cursor to the current input cursor position."""
        width = max((self._console.width or 80) - 1, 20)
        status_lines = self._render_hint_lines()
        panel_lines = self._render_panel_lines(width)
        lines_up = (
            len(self._input_lines)
            - self._cursor_row
            + 1
            + len(panel_lines)
            + (1 if panel_lines else 0)
            + len(status_lines)
        )
        display_line = "*" * len(self._current_line()) if self._active_text_secret else self._current_line()
        prompt_width = 0 if self._active_text_prompt is not None else 2
        cursor = min(self._cursor_col, len(display_line))
        col = prompt_width + cell_len(display_line[:cursor])
        sys.stdout.write(f"\x1b[{lines_up}A\x1b[{col + 1}G")
        if frame_rows is not None:
            self._cursor_to_frame_top_lines = max(frame_rows - lines_up, 0)
            self._cursor_to_frame_end_lines = lines_up
            self._last_frame_rows = frame_rows

    # ── terminal setup ───────────────────────────────────────────────────

    def _setup_terminal(self) -> None:
        if not _HAS_TERMIOS:
            self._tty = False
            return
        if self._stdin_fd is None or not os.isatty(self._stdin_fd):
            return
        self._old_termios = termios.tcgetattr(self._stdin_fd)
        new = termios.tcgetattr(self._stdin_fd)
        # raw mode: no echo, no canonical, no CR->LF translation, VMIN=1 VTIME=0
        # VMIN=1: os.read() blocks until at least 1 byte, then returns
        #          ALL available bytes (escape sequences arrive as one burst)
        new[3] = new[3] & ~(
            termios.ECHO | termios.ICANON | termios.ISIG | termios.IEXTEN
        )
        new[6][termios.VMIN] = 1
        new[6][termios.VTIME] = 0
        # Keep BRKINT so Ctrl+C sends SIGINT as fallback
        new[0] = new[0] & ~(termios.IGNBRK | termios.ICRNL)
        new[0] |= termios.BRKINT
        termios.tcsetattr(self._stdin_fd, termios.TCSADRAIN, new)

    def _restore_terminal(self) -> None:
        if not _HAS_TERMIOS or self._old_termios is None:
            return
        termios.tcsetattr(self._stdin_fd, termios.TCSADRAIN, self._old_termios)

    # ── input reading ────────────────────────────────────────────────────

    async def _read_input_raw(self) -> bytes:
        """Read raw bytes from the terminal (raw mode, VMIN=1)."""
        if self._stdin_fd is None:
            return await self._read_input_line()
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[None] = loop.create_future()
        loop.add_reader(self._stdin_fd, lambda: fut.set_result(None) if not fut.done() else None)
        try:
            await fut
        finally:
            loop.remove_reader(self._stdin_fd)

        # VMIN=1: os.read blocks until >=1 byte arrives, then returns all
        # available bytes. Escape sequences arrive in one burst, so we
        # get the full sequence (e.g. "\x1b[A") in a single read.
        return os.read(self._stdin_fd, 256)

    async def _read_input_line(self) -> bytes:
        """Fallback: read a line from stdin (not a tty)."""
        line = await asyncio.to_thread(sys.stdin.readline)
        if not line:
            return b"\x04"  # Ctrl+D to signal exit
        return line.encode("utf-8", errors="replace")

    @staticmethod
    def _stdin_fileno() -> int | None:
        try:
            return sys.stdin.fileno()
        except (AttributeError, OSError, io.UnsupportedOperation):
            return None

    # ── input processing ─────────────────────────────────────────────────

    def _process_input(self, data: bytes) -> bool:
        if not data:
            return False

        i = 0
        needs_render = False
        while i < len(data):
            consumed, action = self._dispatch_key(data, i)
            if action == "submit":
                needs_render = self._do_submit() or needs_render
            elif action == "interrupt":
                self._handle_interrupt()
                needs_render = True
            elif action == "exit":
                self._request_exit()
                needs_render = True
            elif action == "noop":
                pass
            else:
                needs_render = True
            i += consumed
        return needs_render

    def _dispatch_key(self, data: bytes, offset: int) -> tuple[int, str | None]:
        """Parse a key sequence starting at offset. Returns (bytes_consumed, action)."""
        first = data[offset]

        # UTF-8 multi-byte
        if first >= 0x80:
            ulen = _utf8_len(first)
            if offset + ulen <= len(data):
                ch = data[offset : offset + ulen].decode("utf-8", errors="replace")
                self._insert_text(ch)
                return (ulen, None)
            return (1, None)

        # Ctrl+C
        if first == 0x03:
            return (1, "interrupt")

        # Ctrl+D on empty input
        if first == 0x04:
            if self._is_input_empty():
                return (1, "exit")
            self._delete_forward()
            return (1, None)

        # Enter is CR in raw TTY mode. LF is Ctrl+J there, useful as newline.
        if first == 0x0A:
            if self._tty:
                self._insert_newline()
                return (1, None)
            return (1, "submit")
        if first == 0x0D:
            return (1, "submit")

        # Tab
        if first == 0x09:
            self._handle_tab()
            return (1, None)

        # Backspace
        if first in (0x7F, 0x08):
            self._delete_backward()
            return (1, None)

        # Escape sequences
        if first == 0x1B and offset + 1 < len(data):
            return self._dispatch_escape(data, offset)

        # Plain Escape
        if first == 0x1B:
            self._handle_escape()
            return (1, None)

        # Printable ASCII
        if 0x20 <= first <= 0x7E:
            self._insert_text(bytes([first]).decode("ascii"))
            return (1, None)

        return (1, None)

    def _dispatch_escape(self, data: bytes, offset: int) -> tuple[int, str | None]:
        """Parse escape sequence."""
        seq = data[offset:]  # includes ESC
        end = offset + 1

        # Alt+Enter: ESC + CR → insert newline
        if len(seq) >= 2 and seq[1:2] == b"\r":
            self._insert_newline()
            return (2, None)

        # CSI sequences: ESC [
        if len(seq) >= 3 and seq[1:2] == b"[":
            return self._dispatch_csi(seq, offset)

        # Alt+key: ESC + printable → insert the char (for Alt+Enter we handled above)
        if len(seq) >= 2 and _is_printable(seq[1:2]):
            self._insert_text(seq[1:2].decode("ascii"))
            return (2, None)

        return (1, None)

    def _dispatch_csi(self, seq: bytes, offset: int) -> tuple[int, str | None]:
        """Parse CSI sequence: ESC [ ... final_byte."""
        if len(seq) >= 6 and seq[2:3] == b"M":
            return (6, "noop")

        # Find the end (final byte in range 0x40-0x7E)
        end_idx = 2  # past ESC[
        while end_idx < len(seq) and not (0x40 <= seq[end_idx] <= 0x7E):
            end_idx += 1
        if end_idx >= len(seq):
            return (1, None)  # incomplete

        final = seq[end_idx]
        param_str = seq[2:end_idx].decode("ascii", errors="replace")
        params = param_str.split(";") if param_str else [""]
        consumed = end_idx + 1

        if _is_mouse_csi(final, params):
            return (consumed, "noop")

        # CSI u (kitty keyboard protocol): ESC [ codepoint ; modifiers u
        if final == 0x75:  # 'u'
            return self._dispatch_csi_u(params, consumed)

        # Arrow keys
        if final == 0x41:  # Up
            if self._active_choice is not None:
                self._move_choice(-1)
            elif self._attachment_panel_active():
                self._move_attachment_selection(-1)
            elif self._command_panel_active:
                self._move_command_selection(-1)
            else:
                self._history_prev()
            return (consumed, None)
        if final == 0x42:  # Down
            if self._active_choice is not None:
                self._move_choice(1)
            elif self._attachment_panel_active():
                self._move_attachment_selection(1)
            elif self._command_panel_active:
                self._move_command_selection(1)
            else:
                self._history_next()
            return (consumed, None)
        if final == 0x43:  # Right
            self._cursor_right()
            return (consumed, None)
        if final == 0x44:  # Left
            self._cursor_left()
            return (consumed, None)

        # Home / End
        if final == 0x48:  # Home
            self._cursor_home()
            return (consumed, None)
        if final == 0x46:  # End
            self._cursor_end()
            return (consumed, None)

        # Modified Enter in some terminals: ESC [ 13 ; modifiers ~
        if final == 0x7E and params[0] == "13":
            mod = _parse_csi_modifier(params)
            if _csi_modifier_has_shift(mod):
                self._insert_newline()
                return (consumed, None)
            return (consumed, "submit")

        # xterm modifyOtherKeys format: ESC [ 27 ; modifiers ; codepoint ~
        if final == 0x7E and len(params) >= 3 and params[0] == "27" and params[2] == "13":
            mod = _parse_csi_modifier(params)
            if _csi_modifier_has_shift(mod):
                self._insert_newline()
                return (consumed, None)
            return (consumed, "submit")

        # Delete
        if final == 0x7E and params[0] == "3":
            self._delete_forward()
            return (consumed, None)

        # Shift+Tab
        if final == 0x5A:  # Z
            return (consumed, None)

        return (consumed, None)

    def _dispatch_csi_u(self, params: list[str], consumed: int) -> tuple[int, str | None]:
        """Handle CSI u format: ESC [ codepoint ; modifier u."""
        try:
            codepoint = int(params[0]) if params[0] else 0
        except ValueError:
            return (consumed, None)
        mod = _parse_csi_modifier(params)
        shift = _csi_modifier_has_shift(mod)

        # Shift+Enter
        if codepoint == 13 and shift:
            self._insert_newline()
            return (consumed, None)

        # Regular Enter via CSI u
        if codepoint == 13:
            return (consumed, "submit")

        # Tab
        if codepoint == 9 and not shift:
            self._handle_tab()
            return (consumed, None)

        # Printable
        if 0x20 <= codepoint <= 0x7E:
            self._insert_text(chr(codepoint))
            return (consumed, None)

        return (consumed, None)

    # ── line editing ─────────────────────────────────────────────────────

    def _current_line(self) -> str:
        return self._input_lines[self._cursor_row] if self._input_lines else ""

    def _set_current_line(self, text: str) -> None:
        if self._input_lines:
            self._input_lines[self._cursor_row] = text

    def _insert_text(self, text: str) -> None:
        if self._active_choice is not None:
            quick = text.lower()
            for _, value, _ in self._active_choice:
                if len(value) == 1 and value.lower() == quick:
                    self._finish_choice(value)
                    self.invalidate()
                    return
            for i, choice in enumerate(self._active_choice):
                label, value, _ = choice
                if i > self._choice_selected and (
                    label.lower().startswith(quick) or value.lower().startswith(quick)
                ):
                    self._choice_selected = i
                    self.invalidate()
                    return
            for i, choice in enumerate(self._active_choice):
                label, value, _ = choice
                if label.lower().startswith(quick) or value.lower().startswith(quick):
                    self._choice_selected = i
                    self.invalidate()
                    return
            return

        self._reset_ctrl_c()
        line = self._current_line()
        col = min(self._cursor_col, len(line))
        new_line = line[:col] + text + line[col:]
        self._set_current_line(new_line)
        self._cursor_col = col + len(text)
        self._update_input_panels()

    def _delete_backward(self) -> None:
        if self._active_choice is not None:
            return
        self._reset_ctrl_c()
        if self._cursor_col > 0:
            line = self._current_line()
            col = self._cursor_col
            new_line = line[: col - 1] + line[col:]
            self._set_current_line(new_line)
            self._cursor_col -= 1
        elif self._cursor_row > 0:
            # Join with previous line
            prev_line = self._input_lines[self._cursor_row - 1]
            cur_line = self._current_line()
            new_cursor = len(prev_line)
            self._input_lines[self._cursor_row - 1] = prev_line + cur_line
            del self._input_lines[self._cursor_row]
            self._cursor_row -= 1
            self._cursor_col = new_cursor
        self._update_input_panels()

    def _delete_forward(self) -> None:
        if self._active_choice is not None:
            return
        self._reset_ctrl_c()
        line = self._current_line()
        col = min(self._cursor_col, len(line))
        if col < len(line):
            new_line = line[:col] + line[col + 1 :]
            self._set_current_line(new_line)
        elif self._cursor_row < len(self._input_lines) - 1:
            # Join with next line
            next_line = self._input_lines[self._cursor_row + 1]
            self._input_lines[self._cursor_row] = line + next_line
            del self._input_lines[self._cursor_row + 1]
        self._update_input_panels()

    def _insert_newline(self) -> None:
        if self._active_choice is not None:
            return
        self._reset_ctrl_c()
        line = self._current_line()
        col = min(self._cursor_col, len(line))
        before = line[:col]
        after = line[col:]
        self._input_lines[self._cursor_row] = before
        self._input_lines.insert(self._cursor_row + 1, after)
        self._cursor_row += 1
        self._cursor_col = 0
        self._update_input_panels()

    def _cursor_left(self) -> None:
        if self._active_choice is not None:
            return
        if self._cursor_col > 0:
            self._cursor_col -= 1
        elif self._cursor_row > 0:
            self._cursor_row -= 1
            self._cursor_col = len(self._current_line())
        self._update_input_panels()

    def _cursor_right(self) -> None:
        if self._active_choice is not None:
            return
        line = self._current_line()
        if self._cursor_col < len(line):
            self._cursor_col += 1
        elif self._cursor_row < len(self._input_lines) - 1:
            self._cursor_row += 1
            self._cursor_col = 0
        self._update_input_panels()

    def _cursor_home(self) -> None:
        if self._active_choice is not None:
            return
        self._cursor_col = 0
        self._update_input_panels()

    def _cursor_end(self) -> None:
        if self._active_choice is not None:
            return
        self._cursor_col = len(self._current_line())
        self._update_input_panels()

    # ── history ──────────────────────────────────────────────────────────

    def _history_prev(self) -> None:
        if self._active_choice is not None or not self._input_history:
            return
        self._reset_ctrl_c()
        if self._history_idx == -1:
            self._history_draft = list(self._input_lines)
            self._history_idx = len(self._input_history) - 1
        elif self._history_idx > 0:
            self._history_idx -= 1
        self._load_history_item()

    def _history_next(self) -> None:
        if self._active_choice is not None:
            return
        if self._history_idx == -1:
            return
        self._reset_ctrl_c()
        self._history_idx += 1
        if self._history_idx >= len(self._input_history):
            self._history_idx = -1
            self._input_lines = list(self._history_draft)
            self._cursor_row = len(self._input_lines) - 1
            self._cursor_col = len(self._current_line())
            self._update_input_panels()
            return
        self._load_history_item()

    def _load_history_item(self) -> None:
        if 0 <= self._history_idx < len(self._input_history):
            text = self._input_history[self._history_idx]
            self._input_lines = text.split("\n")
            self._cursor_row = len(self._input_lines) - 1
            self._cursor_col = len(self._current_line())
            self._update_input_panels()

    def _record_history(self, text: str) -> None:
        stripped = text.strip()
        if stripped and (not self._input_history or self._input_history[-1] != stripped):
            self._input_history.append(stripped)
        self._history_idx = -1

    # ── submit ───────────────────────────────────────────────────────────

    def _is_input_empty(self) -> bool:
        return len(self._input_lines) == 1 and not self._input_lines[0]

    def _get_input_text(self) -> str:
        return "\n".join(self._input_lines)

    def _clear_input(self) -> None:
        self._input_lines = [""]
        self._cursor_row = 0
        self._cursor_col = 0
        self._command_panel_active = False
        self._attachment_selected = 0
        self._attachment_panel_suppressed_text = ""

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
        self._record_history(text)
        self._clear_input()
        self._queue.put_nowait(text)
        return True

    def _submit_text_prompt(self) -> None:
        value = self._get_input_text()
        self._text_queue.put_nowait(value)

    def _cancel_text_prompt(self) -> None:
        self._text_queue.put_nowait(None)

    def _handle_escape(self) -> None:
        if self._active_text_prompt is not None:
            self._cancel_text_prompt()
        elif self._active_choice is not None:
            self._finish_choice(None)
        elif self._attachment_panel_active():
            self._attachment_panel_suppressed_text = self._get_input_text()
        elif self._command_panel_active:
            self._command_panel_active = False
        self.invalidate()

    def _handle_tab(self) -> None:
        """Tab completion for commands and slash panel."""
        if self._attachment_panel_active():
            self._accept_attachment_panel_selection()
            return
        line = self._current_line()
        if line.startswith("/"):
            self._reset_ctrl_c()
            filtered = self._filtered_commands()
            if len(filtered) == 1:
                self._input_lines[self._cursor_row] = filtered[0][0]
                self._cursor_col = len(self._input_lines[self._cursor_row])
                self._command_panel_active = False
            elif len(filtered) > 1:
                # Find common prefix
                common = filtered[0][0]
                for name, _ in filtered[1:]:
                    while not name.startswith(common):
                        common = common[:-1]
                if len(common) > len(line):
                    self._input_lines[self._cursor_row] = common
                    self._cursor_col = len(common)
            self.invalidate()

    def _input_cursor_position(self) -> int:
        cursor = 0
        for row in range(min(self._cursor_row, len(self._input_lines))):
            cursor += len(self._input_lines[row]) + 1
        if self._input_lines:
            cursor += min(self._cursor_col, len(self._current_line()))
        return cursor

    def _set_input_text_and_cursor(self, text: str, cursor: int) -> None:
        self._input_lines = text.split("\n") or [""]
        cursor = max(0, min(cursor, len(text)))
        before = text[:cursor]
        self._cursor_row = before.count("\n")
        last_newline = before.rfind("\n")
        self._cursor_col = len(before) if last_newline == -1 else len(before) - last_newline - 1

    def _update_input_panels(self) -> None:
        self._update_command_panel()
        self._clamp_attachment_selection()

    def _update_command_panel(self) -> None:
        line = self._current_line()
        if self._cursor_row == len(self._input_lines) - 1 and line.startswith("/"):
            self._command_panel_active = True
            self._command_selected = 0
        else:
            self._command_panel_active = False

    def _attachment_token(self) -> AttachmentToken | None:
        if self._active_choice is not None or self._active_text_prompt is not None:
            return None
        if self._command_panel_active:
            return None
        return find_attachment_token(self._get_input_text(), self._input_cursor_position())

    def _attachment_panel_active(self) -> bool:
        text = self._get_input_text()
        return (
            self._active_choice is None
            and self._active_text_prompt is None
            and not self._command_panel_active
            and text != self._attachment_panel_suppressed_text
            and self._attachment_token() is not None
        )

    def _attachment_matches(self) -> list[FileCandidate]:
        token = self._attachment_token()
        if token is None:
            return []
        return list_file_candidates(self.status.workspace, token.query, limit=8)

    def _attachment_selectable_count(self) -> int:
        return min(len(self._attachment_matches()), 8)

    def _clamp_attachment_selection(self) -> None:
        count = self._attachment_selectable_count()
        if count <= 0:
            self._attachment_selected = 0
            return
        self._attachment_selected = max(0, min(self._attachment_selected, count - 1))

    def _filtered_commands(self) -> list[tuple[str, str]]:
        line = self._current_line().strip()
        if not line.startswith("/"):
            return []
        p = line.lower()
        return [(n, d) for n, d in self.commands if n.lower().startswith(p) or p.startswith(n.lower())]

    def _move_command_selection(self, delta: int) -> None:
        count = min(len(self._filtered_commands()), 8)
        if count <= 0:
            self._command_selected = 0
            return
        self._command_selected = max(0, min(self._command_selected + delta, count - 1))
        self.invalidate()

    def _move_attachment_selection(self, delta: int) -> None:
        count = self._attachment_selectable_count()
        if count <= 0:
            self._attachment_selected = 0
            return
        self._attachment_selected = max(0, min(self._attachment_selected + delta, count - 1))
        self.invalidate()

    def _accept_command_panel_selection(self) -> bool:
        filtered = self._filtered_commands()
        if not filtered:
            return False
        selected = filtered[min(self._command_selected, len(filtered) - 1)][0]
        text = self._get_input_text().strip()
        if text == selected or text.startswith(selected + " "):
            return False
        self._input_lines = [selected]
        self._cursor_row = 0
        self._cursor_col = len(selected)
        self._command_panel_active = False
        self.invalidate()
        return True

    def _accept_attachment_panel_selection(self) -> bool:
        token = self._attachment_token()
        if token is None:
            return False
        matches = self._attachment_matches()
        if not matches:
            return False
        selected = matches[min(self._attachment_selected, len(matches) - 1)]
        replacement = attachment_token_text(selected.rel_path) + " "
        text = self._get_input_text()
        new_text = text[:token.start] + replacement + text[token.end:]
        new_cursor = token.start + len(replacement)
        self._set_input_text_and_cursor(new_text, new_cursor)
        self._attachment_panel_suppressed_text = ""
        self._attachment_selected = 0
        self.invalidate()
        return True

    def _insert_text_token(self, token: str) -> None:
        self._reset_ctrl_c()
        line = self._current_line()
        col = min(self._cursor_col, len(line))
        self._set_current_line(line[:col] + token + line[col:])
        self._cursor_col = col + len(token)
        self._update_input_panels()

    # ── choice ───────────────────────────────────────────────────────────

    def _move_choice(self, delta: int) -> None:
        if self._active_choice is None:
            return
        n = len(self._active_choice)
        if n == 0:
            return
        self._choice_selected = (self._choice_selected + delta) % n
        self.invalidate()

    def _finish_choice(self, value: str | None) -> None:
        self._choice_queue.put_nowait(value)

    def _submit_choice_selection(self) -> None:
        if self._active_choice is None:
            return
        _, value, _ = self._active_choice[self._choice_selected]
        self._finish_choice(value)

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
                from voidx.ui.events import ErrorAppended, ui_events, via_events

                if via_events():
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

    # ── rendering ────────────────────────────────────────────────────────

    def _render_impl(self) -> Group:
        width = max((self._console.width or 80) - 1, 20)
        height = self._console.height or 24

        status_lines = self._render_hint_lines()
        input_height = len(self._input_lines) + (1 if self._active_text_prompt is not None else 0)

        # Command output
        cmd_output_lines = self._render_command_output(width)

        panel_lines = self._render_panel_lines(width)

        # Total reserved area below transcript
        reserved = (
            input_height
            + len(cmd_output_lines)
            + len(panel_lines)
            + len(status_lines)
            + 2  # transcript/input separator + input/panel separator
            + (1 if panel_lines else 0)
        )

        # Transcript
        body_limit = max(height - reserved, 1)
        tree_lines = dock.tree.render(width)

        # Build renderables
        elements: list = []

        # Transcript (tail of tree)
        visible_tree = tree_lines[-body_limit:] if len(tree_lines) > body_limit else tree_lines
        for line in visible_tree:
            try:
                elements.append(_text_from_line(line))
            except Exception:
                elements.append(Text(line))

        # Separator
        elements.append(Text("─" * (width + 1), style="dim"))

        # Command output
        for line in cmd_output_lines:
            if ANSI_LINE_PREFIX in line:
                try:
                    elements.append(_text_from_line(line))
                    continue
                except Exception:
                    pass
            if line.startswith("[bold]") and line.endswith("[/bold]"):
                try:
                    elements.append(_text_from_line(line))
                    continue
                except Exception:
                    pass
            elements.append(Text(line, style="dim"))

        # Input area
        input_border = "─" * (width + 1)
        prompt = "❯ "

        if self._active_text_prompt is not None:
            elements.append(Text(f"{self._active_text_prompt} ", style="bold"))
            prompt = ""

        # Render input lines: first line gets the prompt prefix,
        # continuation lines get matching indentation.
        prompt_width = 2  # "❯ " width
        for row, line in enumerate(self._input_lines):
            if self._active_text_secret:
                display = "*" * len(line)
            else:
                display = line

            if row == 0:
                prefix = prompt
            else:
                prefix = " " * prompt_width

            if row == self._cursor_row and not self._active_choice:
                col = min(self._cursor_col, len(display))
                before = display[:col]
                at = display[col : col + 1] or " "
                after = display[col + 1 :]

                parts: list = [Text(prefix, style="bold white")]
                if before:
                    parts.append(Text(before, style="white"))
                parts.append(Text(at, style="reverse white"))
                if after:
                    parts.append(Text(after, style="white"))
                elements.append(Text.assemble(*parts))
            else:
                elements.append(Text(f"{prefix}{display}", style="white"))

        elements.append(Text(input_border, style="dim"))

        for line in panel_lines:
            elements.append(Text.from_markup(line))

        if panel_lines:
            elements.append(Text(input_border, style="dim"))

        for line in status_lines:
            elements.append(line)

        return Group(*elements)

    def _render_hint_lines(self) -> list:
        lines: list = []
        status = self._status_summary(max((self._console.width or 80) - 1, 20))
        if status:
            lines.append(Text(status, style="#8F9BA8"))
        if self._notice:
            lines.append(Text("  " + self._notice, style="#8F9BA8"))
        return lines

    def _status_summary(self, width: int) -> str:
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
            if len(summary) <= width:
                return summary
        return _clip("  " + model_text, width)

    def _render_command_output(self, width: int) -> list[str]:
        if not self._command_output_visible or not self._command_output_lines:
            return []
        result: list[str] = []
        if self._command_output_title:
            result.append(f"[bold]{self._command_output_title}[/bold]")
        max_lines = min(len(self._command_output_lines), 12)
        for line in self._command_output_lines[-max_lines:]:
            truncated = line[: width - 4] + "…" if len(line) > width - 2 else line
            result.append(truncated)
        return result

    def _render_choice_overlay(self, width: int) -> list[str]:
        if self._active_choice is None:
            return []
        result: list[str] = []
        result.append(f"[bold yellow]?[/bold yellow] {_escape_markup(self._choice_prompt)}")
        for i, (label, value, desc) in enumerate(self._active_choice):
            marker = "❯" if i == self._choice_selected else " "
            label_text = _escape_markup(label)
            desc_text = _escape_markup(desc)
            if i == self._choice_selected:
                label_text = f"[bold blue]{label_text}[/bold blue]"
            result.append(f"  {marker} {label_text}  {desc_text}")
        for detail in self._choice_details[:8]:
            name = _escape_markup(str(detail.get("name", "")))
            pattern = _escape_markup(str(detail.get("pattern", "")))
            if pattern:
                result.append(f"    [dim]{name}: {pattern}[/dim]")
            elif name:
                result.append(f"    [dim]{name}[/dim]")
        return result

    def _render_text_prompt(self, width: int) -> list[str]:
        if self._active_text_prompt is None:
            return []
        return []

    def _render_panel_lines(self, width: int) -> list[str]:
        lines: list[str] = []
        lines.extend(self._render_command_palette(width))
        lines.extend(self._render_attachment_panel(width))
        lines.extend(self._render_choice_overlay(width))
        lines.extend(self._render_text_prompt(width))
        return lines

    def _render_attachment_panel(self, width: int) -> list[str]:
        if not self._attachment_panel_active():
            return []
        matches = self._attachment_matches()
        token = self._attachment_token()
        query = token.query if token is not None else ""
        detail = f"{len(matches)} match{'es' if len(matches) != 1 else ''}"
        if query:
            detail += f" for @{_escape_markup(query)}"
        result = [
            "[bold]Attach files[/bold]",
            f"[dim]{detail}[/dim]",
        ]
        if not matches:
            result.append("    [dim]No matching files[/dim]")
            return result
        selected = min(self._attachment_selected, len(matches) - 1)
        visible_count = min(len(matches), 8)
        start = max(0, min(selected - visible_count + 1, len(matches) - visible_count))
        visible = matches[start:start + visible_count]
        if start > 0:
            result.append(f"  [dim]... {start} above[/dim]")
        for offset, candidate in enumerate(visible):
            index = start + offset
            marker = "❯" if index == selected else " "
            style = "bold cyan" if index == selected else "dim"
            meta = _candidate_meta(candidate)
            result.append(
                f"  {marker} [{style}]{_escape_markup(candidate.rel_path)}[/{style}]"
                f"[dim]{_escape_markup(meta)}[/dim]"
            )
        remaining = len(matches) - start - len(visible)
        if remaining > 0:
            result.append(f"  [dim]... {remaining} below[/dim]")
        return result

    def _render_command_palette(self, width: int) -> list[str]:
        if not self._command_panel_active:
            return []
        filtered = self._filtered_commands()
        if not filtered:
            return []
        max_items = min(len(filtered), 8)
        result: list[str] = []
        for i in range(max_items):
            name, desc = filtered[i]
            marker = "❯" if i == self._command_selected else " "
            name_style = "bold cyan" if i == self._command_selected else "dim"
            desc_style = "white" if i == self._command_selected else "dim"
            result.append(
                f"  {marker} [{name_style}]{name}[/{name_style}]"
                f"  [{desc_style}]{_escape_markup(desc)}[/{desc_style}]"
            )
        if len(filtered) > max_items:
            result.append(f"  [dim]… and {len(filtered) - max_items} more[/dim]")
        return result

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

    @staticmethod
    def _normalize_choice_detail(item: Any) -> dict[str, Any]:
        if isinstance(item, dict):
            return item
        model_dump = getattr(item, "model_dump", None)
        if callable(model_dump):
            value = model_dump()
            return value if isinstance(value, dict) else {}
        return {}


def _escape_markup(text: str) -> str:
    return text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _safe_status_value(value: object, fallback: str) -> str:
    try:
        text = str(value)
    except Exception:
        return fallback
    return text if text else fallback


def _call_status(func: object, fallback: str) -> str:
    if not callable(func):
        return fallback
    try:
        return _safe_status_value(func(), fallback)
    except Exception:
        return fallback


def _call_bool(func: object) -> bool:
    if not callable(func):
        return False
    try:
        return bool(func())
    except Exception:
        return False


def _call_int(func: object, fallback: int) -> int:
    if not callable(func):
        return fallback
    try:
        return int(func())
    except Exception:
        return fallback


def _rendered_row_count(text: str) -> int:
    if not text:
        return 0
    rows = text.count("\n")
    if not text.endswith("\n"):
        rows += 1
    return rows


def _clip(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[:width - 1] + "…"


def _candidate_meta(candidate: FileCandidate) -> str:
    if candidate.kind == "dir":
        return f"  dir · {candidate.size} items"
    if candidate.kind == "image":
        return f"  image · {format_size(candidate.size)}"
    return f"  file · {format_size(candidate.size)}"
