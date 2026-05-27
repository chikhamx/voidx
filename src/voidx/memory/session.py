"""Session manager — CRUD, message persistence, auto-titling.

Inspired by opencode's session system: typed Info, create/fork/remove,
timestamp-based listing, message hydration.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from voidx.memory.store import _execute_commit, _fetch_all, _fetch_one


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uid() -> str:
    return uuid.uuid4().hex[:12]


# ── models ──────────────────────────────────────────────────────────────

class SessionInfo(BaseModel):
    id: str
    title: str = "New session"
    workspace: str = "."
    model_provider: str = "deepseek"
    model_name: str = "deepseek-v4-pro"
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)
    message_count: int = 0


class MessageRow(BaseModel):
    id: int | None = None  # auto-increment
    session_id: str
    role: str  # system | user | assistant | tool
    content: str = ""
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None
    created_at: str = Field(default_factory=_now)


# ── session CRUD ────────────────────────────────────────────────────────

async def create_session(
    workspace: str = ".",
    provider: str = "deepseek",
    model: str = "deepseek-v4-pro",
) -> SessionInfo:
    sid = _uid()
    now = _now()
    await _execute_commit(
        """INSERT INTO sessions (id, title, workspace, model_provider, model_name, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (sid, "New session", workspace, provider, model, now, now),
    )
    return SessionInfo(
        id=sid, workspace=workspace, model_provider=provider,
        model_name=model, created_at=now, updated_at=now,
    )


async def get_session(session_id: str) -> SessionInfo | None:
    row = await _fetch_one(
        "SELECT * FROM sessions WHERE id = ?", (session_id,)
    )
    if not row:
        return None
    count_row = await _fetch_one(
        "SELECT COUNT(*) as cnt FROM messages WHERE session_id = ?",
        (session_id,),
    )
    return SessionInfo(
        id=row["id"], title=row["title"], workspace=row["workspace"],
        model_provider=row["model_provider"], model_name=row["model_name"],
        created_at=row["created_at"], updated_at=row["updated_at"],
        message_count=count_row["cnt"] if count_row else 0,
    )


async def list_sessions(limit: int = 50) -> list[SessionInfo]:
    rows = await _fetch_all(
        """SELECT s.*, COUNT(m.id) as cnt
           FROM sessions s
           LEFT JOIN messages m ON s.id = m.session_id
           GROUP BY s.id
           ORDER BY s.updated_at DESC
           LIMIT ?""",
        (limit,),
    )
    return [
        SessionInfo(
            id=row["id"], title=row["title"], workspace=row["workspace"],
            model_provider=row["model_provider"], model_name=row["model_name"],
            created_at=row["created_at"], updated_at=row["updated_at"],
            message_count=row["cnt"] or 0,
        )
        for row in rows
    ]


async def update_title(session_id: str, title: str) -> None:
    await _execute_commit(
        "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
        (title, _now(), session_id),
    )


async def touch_session(session_id: str) -> None:
    await _execute_commit(
        "UPDATE sessions SET updated_at = ? WHERE id = ?",
        (_now(), session_id),
    )


async def delete_session(session_id: str) -> None:
    await _execute_commit("DELETE FROM messages WHERE session_id = ?", (session_id,))
    await _execute_commit("DELETE FROM sessions WHERE id = ?", (session_id,))


# ── message persistence ─────────────────────────────────────────────────

async def save_message(msg: MessageRow) -> int:
    """Save a message row. Returns the auto-generated id."""
    cur = await _execute_commit(
        """INSERT INTO messages (session_id, role, content, tool_calls, tool_call_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            msg.session_id,
            msg.role,
            msg.content,
            json.dumps(msg.tool_calls) if msg.tool_calls else None,
            msg.tool_call_id,
            msg.created_at,
        ),
    )
    return cur.lastrowid


async def load_messages(session_id: str) -> list[MessageRow]:
    """Load all messages for a session, ordered by id."""
    rows = await _fetch_all(
        "SELECT * FROM messages WHERE session_id = ? ORDER BY id ASC",
        (session_id,),
    )
    return [
        MessageRow(
            id=row["id"],
            session_id=row["session_id"],
            role=row["role"],
            content=row["content"],
            tool_calls=json.loads(row["tool_calls"]) if row["tool_calls"] else None,
            tool_call_id=row["tool_call_id"],
            created_at=row["created_at"],
        )
        for row in rows
    ]


async def clear_messages(session_id: str) -> None:
    await _execute_commit(
        "DELETE FROM messages WHERE session_id = ?", (session_id,)
    )


async def last_messages(session_id: str, n: int = 20) -> list[MessageRow]:
    """Last N messages for a session."""
    rows = await _fetch_all(
        "SELECT * FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
        (session_id, n),
    )
    rows = list(reversed(rows))
    return [
        MessageRow(
            id=row["id"],
            session_id=row["session_id"],
            role=row["role"],
            content=row["content"],
            tool_calls=json.loads(row["tool_calls"]) if row["tool_calls"] else None,
            tool_call_id=row["tool_call_id"],
            created_at=row["created_at"],
        )
        for row in rows
    ]
