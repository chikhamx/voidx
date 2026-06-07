"""Smart session title generation helpers."""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from voidx.agent.agents import TITLE_PROMPT
from voidx.agent.graph.streaming import extract_text
from voidx.llm.usage import estimate_context_tokens, estimate_message_tokens, extract_token_usage
from voidx.memory.session import (
    count_messages,
    delete_session,
    load_messages,
    update_title,
    update_title_if_current,
)

if TYPE_CHECKING:
    from voidx.agent.graph.contracts import GraphComponentHost
    from voidx.memory.session import MessageRow


TITLE_TIMEOUT_SECONDS = 10.0
TITLE_PROMPT_USER_CHARS = 500
TEMPORARY_TITLE_CHARS = 80
SMART_TITLE_CHARS = 60


class GraphTitleMixin:
    def _invalidate_session_title_generation(self: "GraphComponentHost") -> None:
        self._title_generation += 1
        task = getattr(self, "_title_task", None)
        if task is not None and not task.done():
            task.cancel()
        self._title_task = None

    def _temporary_session_title(self: "GraphComponentHost", text: str) -> str:
        title = _collapse_whitespace(text).strip() or "New session"
        if len(title) > TEMPORARY_TITLE_CHARS:
            return title[: TEMPORARY_TITLE_CHARS - 3].rstrip() + "..."
        return title

    def _schedule_session_title_generation(
        self: "GraphComponentHost",
        session_id: str,
        first_user_text: str,
        temporary_title: str,
    ) -> None:
        self._invalidate_session_title_generation()
        if self.model is None:
            return

        generation_id = self._title_generation
        task = asyncio.create_task(
            self._generate_session_title(
                session_id,
                generation_id,
                first_user_text,
                temporary_title,
            ),
            name=f"voidx-title-{session_id}",
        )
        self._title_task = task
        task.add_done_callback(self._finish_title_task)

    def _finish_title_task(self: "GraphComponentHost", task: asyncio.Task[None]) -> None:
        if getattr(self, "_title_task", None) is task:
            self._title_task = None
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            return

    async def _generate_session_title(
        self: "GraphComponentHost",
        session_id: str,
        generation_id: int,
        first_user_text: str,
        temporary_title: str,
    ) -> None:
        try:
            title = await self._run_title_agent(first_user_text)
        except asyncio.CancelledError:
            raise
        except Exception:
            return

        if not title:
            return
        if not self._can_apply_generated_title(session_id, generation_id, temporary_title):
            return

        applied = await update_title_if_current(session_id, temporary_title, title)
        if applied and self._can_apply_generated_title(session_id, generation_id, temporary_title):
            self._session = self._session.model_copy(update={"title": title})

    async def _run_title_agent(self: "GraphComponentHost", first_user_text: str) -> str | None:
        if self.model is None:
            return None

        messages = [
            SystemMessage(content=TITLE_PROMPT),
            HumanMessage(content=f"First user message:\n\n{first_user_text[:TITLE_PROMPT_USER_CHARS]}"),
        ]
        context_tokens = estimate_context_tokens(messages, self.config.model.model)
        self._usage_stats.update_context(context_tokens)
        result = await asyncio.wait_for(
            self.model.ainvoke(messages),
            timeout=TITLE_TIMEOUT_SECONDS,
        )
        assistant = result if isinstance(result, AIMessage) else AIMessage(content=getattr(result, "content", str(result)))
        try:
            self._usage_stats.record_call(
                extract_token_usage(assistant),
                fallback_input_tokens=context_tokens,
                fallback_output_tokens=estimate_message_tokens(assistant, self.config.model.model),
                messages=messages,
                model=self.config.model.model,
                cache_key=f"{self.config.model.provider}/{self.config.model.model}",
            )
        except Exception:
            pass
        return _sanitize_generated_title(extract_text(assistant))

    def _can_apply_generated_title(
        self: "GraphComponentHost",
        session_id: str,
        generation_id: int,
        temporary_title: str,
    ) -> bool:
        session = getattr(self, "_session", None)
        return (
            generation_id == getattr(self, "_title_generation", -1)
            and session is not None
            and session.id == session_id
            and session.title == temporary_title
        )

    async def regenerate_session_title(self: "GraphComponentHost") -> bool:
        session = getattr(self, "_session", None)
        if session is None:
            return False
        rows = await load_messages(session.id)
        first_user = next((row for row in rows if row.role == "user"), None)
        if first_user is None:
            return False
        first_text = _message_row_title_text(first_user)
        temporary_title = self._temporary_session_title(first_text)
        await update_title(session.id, temporary_title)
        self._session = session.model_copy(update={"title": temporary_title})
        self._schedule_session_title_generation(session.id, first_text, temporary_title)
        return True

    async def _delete_empty_current_session(self: "GraphComponentHost") -> None:
        session = getattr(self, "_session", None)
        if session is None:
            return
        try:
            if await count_messages(session.id) != 0:
                return
            self._invalidate_session_title_generation()
            await delete_session(session.id)
            self._session = None
            self._session_msg_cache = []
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
