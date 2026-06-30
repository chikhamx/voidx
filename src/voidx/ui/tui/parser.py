"""Input parsing — raw byte → key event dispatch."""

from __future__ import annotations

import asyncio
import io
import os
import sys

from voidx.ui.tui.helpers import _is_printable, _utf8_len, _parse_csi_modifier, _csi_modifier_has_shift


class _InputParserMixin:
    """Methods: _read_input_raw, _read_input_line, _stdin_fileno,
    _process_input, _dispatch_key, _dispatch_escape, _dispatch_csi, _dispatch_csi_u."""

    # ── input reading ────────────────────────────────────────────────────

    async def _read_input_raw(self) -> bytes:
        """Read raw bytes from the terminal (raw mode, VMIN=1)."""
        if self._stdin_fd is None:
            return await self._read_input_line()

        if sys.platform == "win32":
            return await self._read_input_raw_win32()

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        loop.add_reader(
            self._stdin_fd,
            lambda: fut.set_result(None) if not fut.done() else None,
        )
        try:
            await fut
        finally:
            loop.remove_reader(self._stdin_fd)

        data = os.read(self._stdin_fd, 4096)
        return data or b"\x04"

    def _close_stdin_reader(self) -> None:
        transport = self._stdin_stream_transport
        if transport is not None:
            transport.close()
        pipe = self._stdin_stream_pipe
        if transport is None and pipe is not None:
            try:
                pipe.close()
            except OSError:
                pass
        self._stdin_stream_reader = None
        self._stdin_stream_transport = None
        self._stdin_stream_pipe = None

    async def _read_input_raw_win32(self) -> bytes:
        """Read raw key input on Windows via msvcrt."""
        import msvcrt

        _WIN_KEY_MAP = {
            "H": "\x1b[A",   # Up
            "P": "\x1b[B",   # Down
            "K": "\x1b[D",   # Left
            "M": "\x1b[C",   # Right
            "G": "\x1b[H",   # Home
            "O": "\x1b[F",   # End
            "I": "\x1b[2~",  # Insert
            "S": "\x1b[3~",  # Delete
            "R": "\x1b[2;2~",  # Shift+Insert
        }

        def _read() -> bytes:
            ch = msvcrt.getwch()
            if ch == "\x00" or ch == "\xe0":
                # Function / arrow key: read the second byte
                ch2 = msvcrt.getwch()
                mapped = _WIN_KEY_MAP.get(ch2)
                if mapped:
                    return mapped.encode("utf-8")
                return ("\x00" + ch2).encode("utf-8")
            # Any character could be the start of a paste. Probe for
            # quickly-following chars; if they look like a paste (multiple
            # newlines or a long run), wrap the whole thing as bracketed
            # paste. This fixes the case where pasted text starts with a
            # regular char (e.g. "line1\r\nline2") — previously only
            # newline-first pastes triggered the drain, so the leading
            # "line1" was inserted char-by-char while the rest became a
            # partial [Pasted text] token.
            pasted = self._try_drain_win32_paste(ch)
            if pasted is not None:
                return pasted
            return ch.encode("utf-8")

        return await asyncio.to_thread(_read)

    def _try_drain_win32_paste(self, first_char: str, timeout_ms: int = 20) -> bytes | None:
        """Drain remaining console input to detect a paste.

        Windows Terminal injects pasted content character-by-character into
        the console input buffer. This method reads any quickly-following
        characters after ``first_char`` and, if they look like a paste
        (multiple newlines or a long run), wraps them in a bracketed-paste
        sequence so ``_process_paste`` handles them as a unit.

        Works with ANY first character, not just newlines — pasted text
        often starts with a regular character (e.g. "line1\\r\\nline2").
        Returns the bracketed-paste bytes, or ``None`` if this is not a paste.
        """
        import msvcrt
        import time

        # Quick exit: if nothing follows immediately, this is a single
        # keypress (normal typing), not a paste. Avoids entering the poll
        # loop for every ordinary character.
        if not msvcrt.kbhit():
            return None

        buffer = first_char
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        while time.monotonic() < deadline:
            if not msvcrt.kbhit():
                time.sleep(0.001)
                continue
            ch = msvcrt.getwch()
            # Function key prefix: not part of paste content. Must consume the
            # second byte, otherwise it lingers in the buffer and becomes a
            # ghost keypress on the next read.
            if ch == "\x00" or ch == "\xe0":
                msvcrt.getwch()
                break
            buffer += ch

        newline_count = buffer.count("\r") + buffer.count("\n")
        if newline_count >= 2 or len(buffer) > 8:
            return b"\x1b[200~" + buffer.encode("utf-8", errors="replace") + b"\x1b[201~"
        # Not a paste: preserve any extra chars we consumed so they are
        # processed on the next _process_input call instead of being lost.
        extra = buffer[1:]
        if extra:
            self._pending_bytes = extra.encode("utf-8", errors="replace")
        return None

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

    _PASTE_START = b"\x1b[200~"
    _PASTE_END = b"\x1b[201~"

    def _process_input(self, data: bytes) -> bool:
        # Prepend any bytes held over from a previous truncated read
        if self._pending_bytes:
            data = self._pending_bytes + data
            self._pending_bytes = b""
        if not data:
            return False

        # ── bracketed paste mode ──────────────────────────────────────────
        if self._paste_buffer is not None:
            return self._process_paste(data)

        # Check if this data starts with or contains a paste-begin sequence
        start_idx = data.find(self._PASTE_START)
        if start_idx != -1:
            # Process any bytes before the paste start normally
            needs_render = False
            if start_idx > 0:
                needs_render = self._process_input(data[:start_idx])
            # Enter paste mode
            self._paste_buffer = b""
            after = data[start_idx + len(self._PASTE_START) :]
            if after:
                needs_render = self._process_paste(after) or needs_render
            return needs_render

        i = 0
        needs_render = False
        input_region_only = True
        while i < len(data):
            consumed, action = self._dispatch_key(data, i)
            if action == "submit":
                if self._do_submit():
                    needs_render = True
                input_region_only = False
            elif action == "interrupt":
                self._handle_interrupt()
                needs_render = True
                input_region_only = False
            elif action == "exit":
                self._request_exit()
                needs_render = True
                input_region_only = False
            elif action == "paste_clipboard":
                self._paste_clipboard_quiet()
                needs_render = True
                input_region_only = False
            elif action == "noop":
                pass
            else:
                needs_render = True
            i += consumed
        self._input_region_render_pending = needs_render and input_region_only
        return needs_render

    def _process_paste(self, data: bytes) -> bool:
        """Process data while in bracketed paste mode."""
        end_idx = data.find(self._PASTE_END)
        if end_idx == -1:
            # Paste continues — accumulate
            self._paste_buffer += data
            return False

        # Paste complete — accumulate up to the end marker
        self._paste_buffer += data[:end_idx]
        remaining = data[end_idx + len(self._PASTE_END) :]

        # Decode and insert the pasted text as a whole
        text = self._paste_buffer.decode("utf-8", errors="replace")
        self._paste_buffer = None
        self._insert_pasted_text(text)

        # Process any remaining bytes after the paste end normally
        if remaining:
            return self._process_input(remaining) or True
        return True

    def _insert_pasted_text(self, text: str) -> None:
        """Insert pasted text, converting line endings to newlines in the editor.

        When the pasted text is empty (e.g. Cmd+V with an image on macOS where
        the terminal sends an empty bracketed-paste), fall back to reading the
        system clipboard for an image.
        """
        if not text:
            self._paste_clipboard_image_quiet()
            return
        # Normalise line endings: \r\n → \n, \r → \n
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        # All pasted text collapses to a [Pasted text #N ...] token, regardless
        # of length. This keeps the input box compact and clearly marks pasted
        # content as a distinct unit.
        #
        # Merge with the previous paste if it happened within a short window.
        # Windows Terminal may split a large paste across multiple drain
        # batches (20ms timeout fires between batches), producing two
        # bracketed-paste sequences. Without merging, the user sees two
        # adjacent tokens for what was a single paste action.
        import time

        now = time.monotonic()
        last = getattr(self, "_last_paste_time", None)
        self._last_paste_time = now

        if (
            last is not None
            and (now - last) < 0.2
            and self._paste_entries
            and self._paste_entries[-1].get("kind") == "text"
        ):
            # Append to the previous paste entry
            entry = self._paste_entries[-1]
            combined = str(entry["expanded"]) + text
            old_display = str(entry["display"])
            new_display = self._compute_text_paste_display(int(entry["id"]), combined)
            entry["expanded"] = combined
            entry["display"] = new_display
            # Replace the old token in the editor with the updated one
            self._replace_token_in_editor(old_display, new_display)
        else:
            self._insert_text_token(self._register_text_paste(text))

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
            # Truncated multi-byte: save remaining bytes for the next read
            self._pending_bytes = data[offset:]
            return (len(data) - offset, None)

        # Ctrl+C
        if first == 0x03:
            return (1, "interrupt")

        # Ctrl+D on empty input
        if first == 0x04:
            if self._is_input_empty():
                return (1, "exit")
            self._delete_forward()
            return (1, None)

        # Ctrl+A / Ctrl+E follow readline-style current-line navigation.
        if first == 0x01:
            self._cursor_home()
            return (1, None)
        if first == 0x05:
            self._cursor_end()
            return (1, None)

        # Enter is CR in raw TTY mode. LF is Ctrl+J there, useful as newline.
        if first == 0x0A:
            if self._tty:
                if self._active_choice is not None or self._active_text_prompt is not None:
                    return (1, "submit")
                self._insert_newline()
                return (1, None)
            return (1, "submit")
        if first == 0x0D:
            return (1, "submit")

        # Ctrl+V: paste clipboard content. Prefer image, fall back to text.
        if first == 0x16:
            return (1, "paste_clipboard")

        # Tab
        if first == 0x09:
            self._handle_tab()
            return (1, None)

        # Backspace
        if first in (0x7F, 0x08):
            self._delete_backward()
            return (1, None)

        # Escape sequences (need at least one more byte to identify)
        if first == 0x1B:
            if offset + 1 < len(data):
                return self._dispatch_escape(data, offset)
            elif len(data) > 1:
                # ESC at end of multi-byte buffer — possible truncation, defer
                self._pending_bytes = data[offset:]
                return (len(data) - offset, None)
            else:
                # Lone ESC in a single-byte read — genuine ESC keypress
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

        if len(seq) >= 3 and seq[1:2] == b"O":
            final = seq[2]
            if final == 0x48:  # SS3 Home
                self._cursor_home()
                return (3, None)
            if final == 0x46:  # SS3 End
                self._cursor_end()
                return (3, None)

        # Alt+key: ESC + printable → insert the char (for Alt+Enter we handled above)
        if len(seq) >= 2 and _is_printable(seq[1:2]):
            if self._active_choice is not None:
                return (2, "noop")
            self._insert_text(seq[1:2].decode("ascii"))
            return (2, None)

        return (1, None)

    def _dispatch_csi(self, seq: bytes, offset: int) -> tuple[int, str | None]:
        """Parse CSI sequence: ESC [ ... final_byte."""
        # Find the end (final byte in range 0x40-0x7E)
        end_idx = 2  # past ESC[
        while end_idx < len(seq) and not (0x40 <= seq[end_idx] <= 0x7E):
            end_idx += 1
        if end_idx >= len(seq):
            self._pending_bytes = seq  # save incomplete CSI for next read
            return (len(seq), None)

        final = seq[end_idx]
        param_str = seq[2:end_idx].decode("ascii", errors="replace")
        params = param_str.split(";") if param_str else [""]
        consumed = end_idx + 1

        # PageUp / PageDown — handled natively by terminal scrollback
        if final == 0x7E and params[0] in ("5", "6"):
            return (consumed, "noop")

        if final == 0x7E and params[0] in ("1", "7"):
            self._cursor_home()
            return (consumed, None)
        if final == 0x7E and params[0] in ("4", "8"):
            self._cursor_end()
            return (consumed, None)

        # CSI u (kitty keyboard protocol): ESC [ codepoint ; modifiers u
        if final == 0x75:  # 'u'
            return self._dispatch_csi_u(params, consumed)

        # Arrow keys
        if final == 0x41:  # Up
            if self._active_choice is not None:
                self._move_choice(-1)
            elif self._skill_panel_active():
                self._move_skill_selection(-1)
            elif self._attachment_panel_active():
                self._move_attachment_selection(-1)
            elif self._command_panel_active:
                self._move_command_selection(-1)
            else:
                self._cursor_up_or_history()
            return (consumed, None)
        if final == 0x42:  # Down
            if self._active_choice is not None:
                self._move_choice(1)
            elif self._skill_panel_active():
                self._move_skill_selection(1)
            elif self._attachment_panel_active():
                self._move_attachment_selection(1)
            elif self._command_panel_active:
                self._move_command_selection(1)
            else:
                self._cursor_down_or_history()
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
            return (consumed, "noop")

        # Unrecognised CSI — silently consume (mouse sequences etc.)
        return (consumed, "noop")

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
