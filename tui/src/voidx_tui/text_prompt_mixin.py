"""Text prompt API for PureTui."""

from __future__ import annotations

import asyncio


class _TextPromptMixin:
    def _init_text_prompt_state(self) -> None:
        self._text_queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._active_text_prompt: str | None = None
        self._active_text_default: str = ""
        self._active_text_secret: bool = False
        self._saved_input_lines: list[str] = [""]
        self._saved_cursor_row: int = 0
        self._saved_cursor_col: int = 0

    async def ask_text(
        self, prompt: str, default: str = "", secret: bool = False, timeout: float | None = None
    ) -> str | None:
        self._reset_queue_for_current_loop("_text_queue")
        self._drain_queue(self._text_queue)
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
            if timeout is None:
                return await self._text_queue.get()
            return await asyncio.wait_for(self._text_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._drain_queue(self._text_queue)
            self._active_text_prompt = None
            self._active_text_default = ""
            self._active_text_secret = False
            self._input_lines = list(self._saved_input_lines)
            self._cursor_row = self._saved_cursor_row
            self._cursor_col = self._saved_cursor_col
            self.invalidate()

