"""Execution-local turn journal persistence adapter."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import hashlib
import json

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from voidx.agent.adapters.persistence.session_models import MessageRow
from voidx.agent.domain.turn_input import ActiveTurnInput
from voidx.llm.message_markers import (
    is_compaction_message,
    is_continuation_message,
    is_context_pressure_message,
    is_guidance_message,
    is_step_hint_message,
)
from voidx.llm.message_status import message_status


def message_journal_key(msg: BaseMessage) -> str:
    """Deterministic commit key for graph messages."""
    msg_id = getattr(msg, "id", None)
    if msg_id:
        return f"id:{msg_id}"
    content = getattr(msg, "content", "")
    tool_calls = getattr(msg, "tool_calls", None) or []
    tool_call_id = getattr(msg, "tool_call_id", None) or ""
    role = msg.__class__.__name__
    payload = json.dumps(
        {
            "role": role,
            "content": str(content),
            "tool_calls": tool_calls,
            "tool_call_id": tool_call_id,
        },
        sort_keys=True,
    )
    return f"hash:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def row_from_graph_message(msg: BaseMessage, session_id: str) -> MessageRow | None:
    """Convert an AIMessage or ToolMessage to a persistable MessageRow."""
    if isinstance(msg, AIMessage):
        raw_content = msg.content
        if isinstance(raw_content, list):
            saved = json.dumps(raw_content, ensure_ascii=False)
            fmt = "structured"
        else:
            saved = str(raw_content)
            fmt = "text"
        return MessageRow(
            session_id=session_id,
            role="assistant",
            content=saved,
            content_format=fmt,
            tool_calls=msg.tool_calls if msg.tool_calls else None,
        )
    if isinstance(msg, ToolMessage):
        status = message_status(getattr(msg, "status", None))
        return MessageRow(
            session_id=session_id,
            role="tool",
            content=str(msg.content),
            tool_call_id=getattr(msg, "tool_call_id", None),
            status=status,
        )
    return None


@dataclass
class TurnJournal:
    """Tracks and flushes messages produced within a single real user turn."""

    active_input: ActiveTurnInput
    committed_keys: set[str] = field(default_factory=set)

    async def flush(
        self,
        messages: list[BaseMessage],
        *,
        session_id: str,
        save_func: Callable[[MessageRow], Awaitable[int]],
    ) -> int:
        """Idempotently persist uncommitted assistant/tool messages.

        Excludes real and synthetic HumanMessages, continuation markers,
        and internal control hints.
        """
        flushed_count = 0
        for msg in messages:
            if isinstance(msg, HumanMessage):
                continue
            if is_compaction_message(msg) or is_continuation_message(msg) or is_context_pressure_message(msg):
                continue
            if is_step_hint_message(msg) or is_guidance_message(msg):
                continue

            key = message_journal_key(msg)
            if key in self.committed_keys:
                continue

            row = row_from_graph_message(msg, session_id=session_id)
            if row is not None:
                await save_func(row)
                self.committed_keys.add(key)
                flushed_count += 1

        return flushed_count
