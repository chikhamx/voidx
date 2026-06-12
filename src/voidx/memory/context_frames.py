"""Compiled LLM context frame snapshots."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel, Field

from voidx.memory.store import _execute_commit, _fetch_all, _now


class ContextFrameRecord(BaseModel):
    id: int | None = None
    session_id: str
    user_message_id: int | None = None
    frame_kind: str = "main"
    agent_persona: str = "voidx"
    provider: str
    model: str
    prefix_hash: str
    frame_hash: str
    message_count: int
    token_estimate: int = 0
    messages: list[dict[str, Any]]
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=_now)


def build_context_frame(
    *,
    session_id: str,
    messages: list[BaseMessage],
    provider: str,
    model: str,
    frame_kind: str = "main",
    agent_persona: str = "voidx",
    user_message_id: int | None = None,
    token_estimate: int = 0,
    metadata: dict[str, Any] | None = None,
) -> ContextFrameRecord:
    serialized = [_serialize_message(message) for message in messages]
    return ContextFrameRecord(
        session_id=session_id,
        user_message_id=user_message_id,
        frame_kind=frame_kind,
        agent_persona=agent_persona,
        provider=provider,
        model=model,
        prefix_hash=_hash_payload(_stable_prefix_payload(serialized)),
        frame_hash=_hash_payload(serialized),
        message_count=len(serialized),
        token_estimate=token_estimate,
        messages=serialized,
        metadata=metadata or {},
    )


async def save_context_frame(record: ContextFrameRecord) -> int:
    cur = await _execute_commit(
        """INSERT INTO context_frames (
               session_id, user_message_id, frame_kind, agent_persona, provider,
               model, prefix_hash, frame_hash, message_count, token_estimate,
               messages_json, metadata_json, created_at
           )
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            record.session_id,
            record.user_message_id,
            record.frame_kind,
            record.agent_persona,
            record.provider,
            record.model,
            record.prefix_hash,
            record.frame_hash,
            record.message_count,
            record.token_estimate,
            json.dumps(record.messages, ensure_ascii=False, sort_keys=True),
            json.dumps(record.metadata, ensure_ascii=False, sort_keys=True),
            record.created_at,
        ),
    )
    return cur.lastrowid


async def save_context_frame_from_messages(
    *,
    session_id: str,
    messages: list[BaseMessage],
    provider: str,
    model: str,
    frame_kind: str = "main",
    agent_persona: str = "voidx",
    user_message_id: int | None = None,
    token_estimate: int = 0,
    metadata: dict[str, Any] | None = None,
) -> int:
    return await save_context_frame(build_context_frame(
        session_id=session_id,
        messages=messages,
        provider=provider,
        model=model,
        frame_kind=frame_kind,
        agent_persona=agent_persona,
        user_message_id=user_message_id,
        token_estimate=token_estimate,
        metadata=metadata,
    ))


async def load_context_frames(session_id: str, limit: int = 50) -> list[ContextFrameRecord]:
    rows = await _fetch_all(
        """SELECT * FROM context_frames
           WHERE session_id = ?
           ORDER BY id DESC
           LIMIT ?""",
        (session_id, limit),
    )
    records = [
        ContextFrameRecord(
            id=row["id"],
            session_id=row["session_id"],
            user_message_id=row["user_message_id"],
            frame_kind=row["frame_kind"],
            agent_persona=row["agent_persona"],
            provider=row["provider"],
            model=row["model"],
            prefix_hash=row["prefix_hash"],
            frame_hash=row["frame_hash"],
            message_count=row["message_count"],
            token_estimate=row["token_estimate"],
            messages=json.loads(row["messages_json"]),
            metadata=json.loads(row["metadata_json"]),
            created_at=row["created_at"],
        )
        for row in rows
    ]
    return list(reversed(records))


def _serialize_message(message: BaseMessage) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "role": _message_role(message),
        "content": _json_safe(getattr(message, "content", "")),
    }
    if isinstance(message, AIMessage) and getattr(message, "tool_calls", None):
        payload["tool_calls"] = _json_safe(message.tool_calls)
    if isinstance(message, ToolMessage):
        payload["tool_call_id"] = getattr(message, "tool_call_id", "")
    return payload


def _message_role(message: BaseMessage) -> str:
    if isinstance(message, SystemMessage):
        return "system"
    if isinstance(message, HumanMessage):
        return "user"
    if isinstance(message, AIMessage):
        return "assistant"
    if isinstance(message, ToolMessage):
        return "tool"
    return getattr(message, "type", message.__class__.__name__)


def _stable_prefix_payload(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not messages or messages[0].get("role") != "system":
        return []
    content = str(messages[0].get("content", ""))
    stable_content = content.split("\n\n## Long Summary", 1)[0]
    return [{"role": "system", "content": stable_content}]


def _hash_payload(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))
