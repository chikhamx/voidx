"""Choice prompt API for PureTui."""

from __future__ import annotations

import asyncio
from typing import Any


class _ChoicePromptMixin:
    def _init_choice_prompt_state(self) -> None:
        self._choice_queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._active_choice: list[tuple[str, str, str]] | None = None
        self._choice_prompt: str = ""
        self._choice_selected: int = 0
        self._choice_details: list[dict[str, Any]] = []
        self._choice_anchor: str = ""

    async def ask_choice(
        self,
        prompt: str,
        choices: list[tuple[str, str, str]],
        selected: int = 0,
        anchor: str = "",
        details: list[dict[str, Any]] | None = None,
        timeout: float | None = None,
    ) -> str | None:
        self._reset_queue_for_current_loop("_choice_queue")
        self._drain_queue(self._choice_queue)
        self._choice_prompt = prompt
        self._active_choice = choices
        self._choice_selected = max(0, min(selected, len(choices) - 1))
        self._choice_details = [self._normalize_choice_detail(item) for item in (details or [])]
        self._choice_anchor = anchor
        self.invalidate()
        try:
            if timeout is None:
                return await self._choice_queue.get()
            return await asyncio.wait_for(self._choice_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._drain_queue(self._choice_queue)
            self._active_choice = None
            self._choice_selected = 0
            self._choice_details = []
            self._choice_anchor = ""
            self.invalidate()

