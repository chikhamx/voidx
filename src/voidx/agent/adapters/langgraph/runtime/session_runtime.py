"""Composition component for graph session runtime concerns."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from voidx.agent.application.session_service import SessionService
from voidx.agent.domain.state import SessionRuntimeState
from voidx.agent.adapters.persistence.memory_session import MemorySessionAdapter
from voidx.agent.adapters.persistence.session_repository import MessageRow, count_messages, delete_session, load_messages, update_title, update_title_if_current
from voidx.agent.ports.presentation import NullPresentationSnapshotPort, PresentationSnapshotPort
from voidx.observability.tool_log import log_tool_event
from voidx.agent.adapters.tools.result_storage import cleanup_session_results

TITLE_TIMEOUT_SECONDS = 10.0
TITLE_PERSONA_USER_CHARS = 500
TEMPORARY_TITLE_CHARS = 80
SMART_TITLE_CHARS = 60


class SessionRuntime:
    """Owns persisted runtime state, transcript snapshots, and title generation."""

    def __init__(
        self,
        host: Any,
        *,
        presentation_snapshots: PresentationSnapshotPort | None = None,
    ) -> None:
        self.host = host
        self.presentation_snapshots = presentation_snapshots or NullPresentationSnapshotPort()

    def reset_runtime_state_memory(self) -> None:
        from voidx.agent.application.runtime_context import InteractionMode
        from voidx.agent.domain.task.state import TaskState

        host = self.host
        host._interaction_mode = InteractionMode.AUTO
        host._task_state = TaskState()
        host._compaction_summary = ""
        host._pending_summary = None
        successful_calls = getattr(host, "_successful_dangerous_calls", None)
        if successful_calls is not None:
            successful_calls.clear()
        if hasattr(host, "_successful_dangerous_calls_session_id"):
            host._successful_dangerous_calls_session_id = None
        if not hasattr(host, "_file_read_coverage"):
            host._file_read_coverage = {}
        else:
            host._file_read_coverage.clear()
        if not hasattr(host, "_file_mtimes"):
            host._file_mtimes = {}
        else:
            host._file_mtimes.clear()
        if not hasattr(host, "_workflow_repeat_tracker"):
            host._workflow_repeat_tracker = {}
        else:
            host._workflow_repeat_tracker.clear()

    async def restore_runtime_state(self) -> None:
        host = self.host
        if host._session is None:
            return
        runtime = await SessionService(MemorySessionAdapter()).restore_runtime(host._session.id)
        host._interaction_mode = runtime.interaction_mode
        host._task_state = runtime.task_state
        host._compaction_summary = runtime.compaction_summary
        if runtime.session_time:
            host._session_date = runtime.session_time

    async def persist_runtime_state(self) -> None:
        host = self.host
        if host._session is None:
            return
        from voidx.agent.application.runtime_context import InteractionMode
        from voidx.agent.domain.task.state import TaskState

        runtime = SessionRuntimeState(
            interaction_mode=getattr(host, "_interaction_mode", None) or InteractionMode.AUTO,
            task_state=getattr(host, "_task_state", None) or TaskState(),
            compaction_summary=getattr(host, "_compaction_summary", ""),
            session_time=getattr(host, "_session_date", ""),
        )
        await SessionService(MemorySessionAdapter()).persist_runtime(host._session.id, runtime)

    async def clear_runtime_state(self, *, reset_runtime_state_memory: Callable[[], None] | None = None) -> None:
        host = self.host
        if host._session is not None:
            session_id = host._session.id
            await SessionService(MemorySessionAdapter()).clear_runtime(session_id)
            cleanup_session_results(session_id, workspace=host._workspace)
        reset = reset_runtime_state_memory or self.reset_runtime_state_memory
        reset()

    async def persist_transcript_snapshot(self) -> None:
        host = self.host
        if host._session is None:
            return
        await self.presentation_snapshots.persist_current(host._session.id)

    async def restore_transcript_snapshot(self, *, append: bool = False) -> bool:
        host = self.host
        if host._session is None:
            return False
        return await self.presentation_snapshots.restore_current(
            host._session.id,
            append=append,
        )

    def invalidate_session_title_generation(self) -> None:
        host = self.host
        host._title_generation += 1
        task = getattr(host, "_title_task", None)
        if task is not None and not task.done():
            task.cancel()
        host._title_task = None

    def temporary_session_title(self, text: str) -> str:
        title = _collapse_whitespace(text).strip() or "New session"
        if len(title) > TEMPORARY_TITLE_CHARS:
            return title[: TEMPORARY_TITLE_CHARS - 3].rstrip() + "..."
        return title

    def schedule_session_title_generation(
        self,
        session_id: str,
        first_user_text: str,
        temporary_title: str,
        *,
        invalidate_session_title_generation: Callable[[], None] | None = None,
        generate_session_title: Callable[[str, int, str, str], asyncio.Future | asyncio.Task | object] | None = None,
        finish_title_task: Callable[[asyncio.Task[None]], None] | None = None,
    ) -> None:
        invalidate = invalidate_session_title_generation or self.invalidate_session_title_generation
        invalidate()
        del session_id, first_user_text, temporary_title, generate_session_title, finish_title_task

    def finish_title_task(self, task: asyncio.Task[None]) -> None:
        host = self.host
        if getattr(host, "_title_task", None) is task:
            host._title_task = None
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log_tool_event("session_title_task_failed", message=str(exc))
            return

    async def generate_session_title(
        self,
        session_id: str,
        generation_id: int,
        first_user_text: str,
        temporary_title: str,
        *,
        run_title_agent: Callable[[str], asyncio.Future | asyncio.Task | object] | None = None,
        can_apply_generated_title: Callable[[str, int, str], bool] | None = None,
    ) -> None:
        run_agent = run_title_agent or self.run_title_agent
        can_apply = can_apply_generated_title or self.can_apply_generated_title
        try:
            title = await run_agent(first_user_text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_tool_event("session_title_generation_failed", message=str(exc))
            return

        if not title:
            return

        applied = await update_title_if_current(session_id, temporary_title, title)
        if applied and can_apply(session_id, generation_id, temporary_title):
            host = self.host
            host._session = host._session.model_copy(update={"title": title})

    async def run_title_agent(self, first_user_text: str) -> str | None:
        del first_user_text
        return None

    def can_apply_generated_title(
        self,
        session_id: str,
        generation_id: int,
        temporary_title: str,
    ) -> bool:
        host = self.host
        session = getattr(host, "_session", None)
        return (
            generation_id == getattr(host, "_title_generation", -1)
            and session is not None
            and session.id == session_id
            and session.title == temporary_title
        )

    async def regenerate_session_title(
        self,
        *,
        temporary_session_title: Callable[[str], str] | None = None,
        schedule_session_title_generation: Callable[[str, str, str], None] | None = None,
    ) -> bool:
        host = self.host
        session = getattr(host, "_session", None)
        if session is None:
            return False
        rows = await load_messages(session.id)
        first_user = next((row for row in rows if row.role == "user"), None)
        if first_user is None:
            return False
        first_text = _message_row_title_text(first_user)
        make_temporary = temporary_session_title or self.temporary_session_title
        schedule = schedule_session_title_generation or self.schedule_session_title_generation
        temporary_title = make_temporary(first_text)
        await update_title(session.id, temporary_title)
        host._session = session.model_copy(update={"title": temporary_title})
        schedule(session.id, first_text, temporary_title)
        return True

    async def delete_empty_current_session(
        self,
        *,
        invalidate_session_title_generation: Callable[[], None] | None = None,
    ) -> None:
        host = self.host
        session = getattr(host, "_session", None)
        if session is None:
            return
        try:
            if await count_messages(session.id) != 0:
                return
            invalidate = invalidate_session_title_generation or self.invalidate_session_title_generation
            invalidate()
            await delete_session(session.id)
            host._session = None
            host._session_msg_cache = []
        except Exception as exc:
            log_tool_event("session_cleanup_failed", message=str(exc))
            return


_MARKDOWN_TITLE_RE = re.compile(
    r"(^#{1,6}\s+|^[-*+]\s+|(^|\s)(\*\*|__)[^*_]+(\*\*|__)($|\s)|`[^`]+`|\[[^\]]+\]\([^)]+\))"
)


def _sanitize_generated_title(raw: str) -> str:
    title = _collapse_whitespace(raw).strip().strip("\"'").strip()
    if not title or "```" in title or _MARKDOWN_TITLE_RE.search(title):
        return ""
    if len(title) > SMART_TITLE_CHARS:
        return title[: SMART_TITLE_CHARS - 3].rstrip() + "..."
    return title


def _message_row_title_text(row: "MessageRow") -> str:
    content = row.content
    if row.content_format != "structured":
        return content
    try:
        import json

        parsed = json.loads(content)
    except Exception:
        return content
    if not isinstance(parsed, list):
        return content
    parts: list[str] = []
    for item in parsed:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text", "")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts) or content


def _collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip()
