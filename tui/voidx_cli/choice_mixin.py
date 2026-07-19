"""Choice prompt API for PureTui."""

from __future__ import annotations

import asyncio
from typing import Any


def _normalize_choices(choices: list[str | tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """Normalize mixed choice format to (label, value, desc) tuples."""
    result: list[tuple[str, str, str]] = []
    for choice in choices:
        if isinstance(choice, str):
            result.append((choice, choice, ""))
        else:
            result.append(choice)
    return result


class _ChoicePromptMixin:
    def _init_choice_prompt_state(self) -> None:
        self._choice_queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._choice_state.prompt_lock = asyncio.Lock()
        self._active_choice: list[tuple[str, str, str]] | None = None
        self._choice_prompt: str = ""
        self._choice_selected: int = 0
        self._choice_details: list[dict[str, Any]] = []
        self._choice_anchor: str = ""

    def _prompt_lock_for_current_loop(self) -> asyncio.Lock:
        lock = self._choice_state.prompt_lock
        bound_loop = getattr(lock, "_loop", None)
        if bound_loop is not None and bound_loop is not asyncio.get_running_loop():
            lock = asyncio.Lock()
            self._choice_state.prompt_lock = lock
        return lock

    async def ask_choice(
        self,
        prompt: str,
        choices: list[str | tuple[str, str, str]],
        selected: int = 0,
        anchor: str = "",
        details: list[dict[str, Any]] | None = None,
        timeout: float | None = None,
    ) -> str | None:
        async with self._prompt_lock_for_current_loop():
            normalized = _normalize_choices(choices)
            self._reset_queue_for_current_loop("_choice_queue")
            self._drain_queue(self._choice_queue)
            self._choice_prompt = prompt
            self._active_choice = normalized
            self._choice_selected = max(0, min(selected, len(normalized) - 1))
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
                self._clear_choice_prompt()

    def _clear_choice_prompt(self) -> None:
        self._drain_queue(self._choice_queue)
        self._active_choice = None
        self._choice_selected = 0
        self._choice_details = []
        self._choice_anchor = ""
        self.invalidate()
