"""Hydrate persisted message rows into LangChain messages."""

from __future__ import annotations

from collections.abc import Iterable
import hashlib
import json
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from voidx.agent.application.attachments import parse_structured_content
from voidx.llm.message_status import message_status
from voidx.agent.adapters.persistence.session_repository import MessageRow
from voidx.agent.application.runtime_context import RowMessageCacheEntry
from voidx.llm.message_markers import GUIDANCE_MARKER


def is_guidance_row(row: MessageRow) -> bool:
    return bool(row.additional_kwargs.get(GUIDANCE_MARKER))


def is_user_turn_row(row: MessageRow) -> bool:
    return row.role == "user" and not is_guidance_row(row)


def messages_from_rows(rows: Iterable[MessageRow]) -> list[BaseMessage]:
    messages: list[BaseMessage] = []
    for row in rows:
        msg = message_from_row(row)
        if msg is not None:
            messages.append(msg)
    return messages


def messages_from_rows_incremental(
    rows: Iterable[MessageRow],
    cache: dict[int, RowMessageCacheEntry],
) -> tuple[list[BaseMessage], dict[int, RowMessageCacheEntry]]:
    messages: list[BaseMessage] = []
    next_cache: dict[int, RowMessageCacheEntry] = {}

    for row in rows:
        msg_id = row.id
        fingerprint = row_fingerprint(row)
        cached = cache.get(msg_id) if msg_id is not None else None
        if cached is not None and cached.fingerprint == fingerprint:
            msg = cached.message
        else:
            msg = message_from_row(row)
        if msg is None:
            continue
        messages.append(msg)
        if msg_id is not None:
            next_cache[msg_id] = RowMessageCacheEntry(fingerprint=fingerprint, message=msg)

    return messages, next_cache


def row_fingerprint(row: MessageRow) -> str:
    payload = {
        "role": row.role,
        "content": row.content,
        "content_format": row.content_format,
        "tool_calls": row.tool_calls or [],
        "tool_call_id": row.tool_call_id or "",
        "status": message_status(row.status) if row.role == "tool" else "success",
        "additional_kwargs": row.additional_kwargs,
    }
    return _stable_hash(payload)


def message_from_row(row: MessageRow) -> BaseMessage | None:
    msg_id = str(row.id) if row.id is not None else None
    if row.role == "system":
        return SystemMessage(content=row.content, id=msg_id)
    if row.role == "user":
        return HumanMessage(
            content=parse_structured_content(row.content, row.content_format),
            additional_kwargs=row.additional_kwargs,
            id=msg_id,
        )
    if row.role == "assistant":
        return AIMessage(
            content=parse_structured_content(row.content, row.content_format),
            tool_calls=row.tool_calls or [],
            additional_kwargs=row.additional_kwargs,
            id=msg_id,
        )
    if row.role == "tool":
        return ToolMessage(
            content=row.content,
            tool_call_id=row.tool_call_id or "",
            status=message_status(row.status),
            id=msg_id,
        )
    return None


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
