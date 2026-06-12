"""Smart session title generation proxies."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from voidx.agent.graph.session_mixin import _session_runtime_for
from voidx.agent.graph.session_runtime import (
    SMART_TITLE_CHARS,
    TEMPORARY_TITLE_CHARS,
    TITLE_PERSONA_USER_CHARS,
    TITLE_TIMEOUT_SECONDS,
    _collapse_whitespace,
    _message_row_title_text,
    _sanitize_generated_title,
)

if TYPE_CHECKING:
    from voidx.agent.graph.contracts import GraphRunLoopHost


class GraphTitleMixin:
    def _invalidate_session_title_generation(self: "GraphRunLoopHost") -> None:
        _session_runtime_for(self).invalidate_session_title_generation()

    def _temporary_session_title(self: "GraphRunLoopHost", text: str) -> str:
        return _session_runtime_for(self).temporary_session_title(text)

    def _schedule_session_title_generation(
        self: "GraphRunLoopHost",
        session_id: str,
        first_user_text: str,
        temporary_title: str,
    ) -> None:
        _session_runtime_for(self).schedule_session_title_generation(
            session_id,
            first_user_text,
            temporary_title,
            invalidate_session_title_generation=self._invalidate_session_title_generation,
            generate_session_title=self._generate_session_title,
            finish_title_task=self._finish_title_task,
        )

    def _finish_title_task(self: "GraphRunLoopHost", task: asyncio.Task[None]) -> None:
        _session_runtime_for(self).finish_title_task(task)

    async def _generate_session_title(
        self: "GraphRunLoopHost",
        session_id: str,
        generation_id: int,
        first_user_text: str,
        temporary_title: str,
    ) -> None:
        await _session_runtime_for(self).generate_session_title(
            session_id,
            generation_id,
            first_user_text,
            temporary_title,
            run_title_agent=self._run_title_agent,
            can_apply_generated_title=self._can_apply_generated_title,
        )

    async def _run_title_agent(self: "GraphRunLoopHost", first_user_text: str) -> str | None:
        return await _session_runtime_for(self).run_title_agent(first_user_text)

    def _can_apply_generated_title(
        self: "GraphRunLoopHost",
        session_id: str,
        generation_id: int,
        temporary_title: str,
    ) -> bool:
        return _session_runtime_for(self).can_apply_generated_title(
            session_id,
            generation_id,
            temporary_title,
        )

    async def regenerate_session_title(self: "GraphRunLoopHost") -> bool:
        return await _session_runtime_for(self).regenerate_session_title(
            temporary_session_title=self._temporary_session_title,
            schedule_session_title_generation=self._schedule_session_title_generation,
        )

    async def _delete_empty_current_session(self: "GraphRunLoopHost") -> None:
        await _session_runtime_for(self).delete_empty_current_session(
            invalidate_session_title_generation=self._invalidate_session_title_generation,
        )


__all__ = [
    "GraphTitleMixin",
    "SMART_TITLE_CHARS",
    "TEMPORARY_TITLE_CHARS",
    "TITLE_PERSONA_USER_CHARS",
    "TITLE_TIMEOUT_SECONDS",
    "_collapse_whitespace",
    "_message_row_title_text",
    "_sanitize_generated_title",
]
