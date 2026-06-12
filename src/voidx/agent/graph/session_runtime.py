"""Composition component for graph session runtime concerns."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import TYPE_CHECKING

from voidx.memory.runtime_state import (
    RuntimeStateSnapshot,
    clear_runtime_state,
    load_runtime_state,
    save_runtime_state,
)
from voidx.memory.session import (
    count_messages,
    delete_session,
    load_messages,
    update_title,
    update_title_if_current,
)
from voidx.memory.transcript import load_transcript, replace_transcript
from voidx.ui.transcript import transcript_rows_to_tree, tree_to_transcript_rows

if TYPE_CHECKING:
    from voidx.agent.graph.contracts import GraphRunLoopHost
    from voidx.memory.session import MessageRow


TITLE_TIMEOUT_SECONDS = 10.0
TITLE_PERSONA_USER_CHARS = 500
TEMPORARY_TITLE_CHARS = 80
SMART_TITLE_CHARS = 60


class GraphSessionRuntime:
    """Owns persisted runtime state, transcript snapshots, and title generation."""

    def __init__(self, host: GraphRunLoopHost) -> None:
        self.host = host

    def reset_runtime_state_memory(self) -> None:
        from voidx.agent.runtime_context import InteractionMode
        from voidx.agent.task_state import TaskState

        host = self.host
        host._interaction_mode = InteractionMode.AUTO
        host._task_state = TaskState()
        host._compaction_summary = ""
        host._pending_summary = None

    async def restore_runtime_state(self) -> None:
        host = self.host
        if host._session is None:
            return
        snapshot = await load_runtime_state(host._session.id)
        host._interaction_mode = snapshot.interaction_mode
        host._task_state = snapshot.task_state
        host._compaction_summary = snapshot.compaction_summary
        if snapshot.session_time:
            host._session_date = snapshot.session_time

    async def persist_runtime_state(self) -> None:
        host = self.host
        if host._session is None:
            return
        from voidx.agent.runtime_context import InteractionMode
        from voidx.agent.task_state import TaskState

        interaction_mode = getattr(host, "_interaction_mode", None) or InteractionMode.AUTO
        task_state = getattr(host, "_task_state", None) or TaskState()
        await save_runtime_state(
            host._session.id,
            RuntimeStateSnapshot(
                interaction_mode=interaction_mode,
                task_state=task_state,
                compaction_summary=getattr(host, "_compaction_summary", ""),
                session_time=getattr(host, "_session_date", ""),
            ),
        )

    async def clear_runtime_state(self, *, reset_runtime_state_memory: Callable[[], None] | None = None) -> None:
        host = self.host
        if host._session is not None:
            await clear_runtime_state(host._session.id)
        reset = reset_runtime_state_memory or self.reset_runtime_state_memory
        reset()

    async def persist_transcript_snapshot(self) -> None:
        host = self.host
        if host._session is None:
            return
        active_dock = host._ui.get_dock()
        if active_dock is None:
            return
        rows, turn_count = tree_to_transcript_rows(host._session.id, active_dock.tree)
        await replace_transcript(host._session.id, rows, turn_count=turn_count)

    async def restore_transcript_snapshot(self, *, append: bool = False) -> bool:
        host = self.host
        if host._session is None:
            return False
        active_dock = host._ui.get_dock()
        if active_dock is None:
            return False
        rows = await load_transcript(host._session.id)
        if not rows:
            return False
        active_dock.restore_tree(transcript_rows_to_tree(rows), append=append)
        return True

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
        except Exception:
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
        except Exception:
            return

        if not title:
            return
        if not can_apply(session_id, generation_id, temporary_title):
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
        except Exception:
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
