"""Session manager — CRUD, message persistence, auto-titling.

Inspired by opencode's session system: typed Info, create/fork/remove,
timestamp-based listing, message hydration.
"""

from __future__ import annotations

import asyncio
import shutil
import uuid

from pydantic import BaseModel, Field

from voidx.llm.domain.model import DEFAULT_MODEL
from voidx.llm.message_status import message_status
from voidx.persistence.jsonl import append_session_record, drop_session_lock, read_session_records, session_dir
from voidx.persistence.sqlite import execute_commit, fetch_all, fetch_one, now, write_transaction


def _uid() -> str:
    return uuid.uuid4().hex[:12]


# ── models ──────────────────────────────────────────────────────────────

class SessionInfo(BaseModel):
    id: str
    title: str = "New session"
    workspace: str = "."
    directory: str = ""
    model_provider: str = "anthropic"
    model_name: str = DEFAULT_MODEL
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)
    message_count: int = 0
    runtime_profile: str = "coding"


RUNTIME_PROFILES = ("coding", "chat", "loop", "goal")


def validate_runtime_profile(profile: str) -> str:
    if profile not in RUNTIME_PROFILES:
        raise ValueError(f"unknown runtime profile: {profile}")
    return profile


class MessageRow(BaseModel):
    id: int | None = None  # auto-increment
    session_id: str
    role: str  # system | user | assistant | tool
    content: str = ""
    content_format: str = "text"  # "text" | "structured" (e.g. DeepSeek thinking blocks)
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None
    status: str | None = None
    created_at: str = Field(default_factory=now)


# ── session CRUD ────────────────────────────────────────────────────────

async def create_session(
    workspace: str = ".",
    provider: str = "anthropic",
    model: str = DEFAULT_MODEL,
    *,
    title: str = "New session",
    directory: str = "",
    profile: str = "coding",
) -> SessionInfo:
    profile = validate_runtime_profile(profile)
    sid = _uid()
    timestamp = now()
    await execute_commit(
        """INSERT INTO sessions (id, title, workspace, directory, model_provider, model_name, runtime_profile, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (sid, title, workspace, directory, provider, model, profile, timestamp, timestamp),
    )
    return SessionInfo(
        id=sid, title=title, workspace=workspace, directory=directory,
        model_provider=provider, model_name=model, runtime_profile=profile,
        created_at=timestamp, updated_at=timestamp,
    )


async def ensure_session(
    session_id: str,
    workspace: str,
    *,
    profile: str = "coding",
    title: str = "Loop session",
) -> None:
    """Insert a session row if missing so FK references from loop threads hold."""
    profile = validate_runtime_profile(profile)
    timestamp = now()
    await execute_commit(
        """INSERT OR IGNORE INTO sessions (id, title, workspace, directory, model_provider, model_name, runtime_profile, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (session_id, title, workspace, workspace, "anthropic", DEFAULT_MODEL, profile, timestamp, timestamp),
    )


async def get_session(session_id: str) -> SessionInfo | None:
    row = await fetch_one(
        "SELECT * FROM sessions WHERE id = ?", (session_id,)
    )
    if not row:
        return None
    return SessionInfo(
        id=row["id"], title=row["title"], workspace=row["workspace"],
        directory=row["directory"],
        model_provider=row["model_provider"], model_name=row["model_name"],
        created_at=row["created_at"], updated_at=row["updated_at"],
        message_count=row["message_count"], runtime_profile=row["runtime_profile"],
    )


async def list_sessions(limit: int = 50) -> list[SessionInfo]:
    rows = await fetch_all(
        """SELECT *
           FROM sessions
           ORDER BY updated_at DESC
           LIMIT ?""",
        (limit,),
    )
    return [
        SessionInfo(
            id=row["id"], title=row["title"], workspace=row["workspace"],
            directory=row["directory"],
            model_provider=row["model_provider"], model_name=row["model_name"],
            created_at=row["created_at"], updated_at=row["updated_at"],
            message_count=row["message_count"], runtime_profile=row["runtime_profile"],
        )
        for row in rows
    ]


async def latest_session_for_workspace(workspace: str) -> SessionInfo | None:
    row = await fetch_one(
        """SELECT *
           FROM sessions
           WHERE workspace = ?
           ORDER BY updated_at DESC
           LIMIT 1""",
        (workspace,),
    )
    if not row:
        return None
    return SessionInfo(
        id=row["id"], title=row["title"], workspace=row["workspace"],
        directory=row["directory"],
        model_provider=row["model_provider"], model_name=row["model_name"],
        created_at=row["created_at"], updated_at=row["updated_at"],
        message_count=row["message_count"], runtime_profile=row["runtime_profile"],
    )


async def update_title(session_id: str, title: str, *, touch: bool = True) -> None:
    if touch:
        await execute_commit(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            (title, now(), session_id),
        )
        return
    await execute_commit(
        "UPDATE sessions SET title = ? WHERE id = ?",
        (title, session_id),
    )


async def update_title_if_current(
    session_id: str,
    expected_title: str,
    title: str,
    *,
    touch: bool = True,
) -> bool:
    if touch:
        cur = await execute_commit(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ? AND title = ?",
            (title, now(), session_id, expected_title),
        )
        return cur.rowcount > 0
    cur = await execute_commit(
        "UPDATE sessions SET title = ? WHERE id = ? AND title = ?",
        (title, session_id, expected_title),
    )
    return cur.rowcount > 0


async def update_session_profile(session_id: str, profile: str) -> None:
    await execute_commit(
        "UPDATE sessions SET runtime_profile = ?, updated_at = ? WHERE id = ?",
        (profile, now(), session_id),
    )


async def update_session_model(session_id: str, provider: str, model: str) -> None:
    await execute_commit(
        "UPDATE sessions SET model_provider = ?, model_name = ?, updated_at = ? WHERE id = ?",
        (provider, model, now(), session_id),
    )


async def touch_session(session_id: str) -> None:
    await execute_commit(
        "UPDATE sessions SET updated_at = ? WHERE id = ?",
        (now(), session_id),
    )


async def fork_session(
    session_id: str,
    *,
    title: str | None = None,
) -> SessionInfo | None:
    """Create a new session by forking an existing one.

    Copies workspace/provider/model from the source session. The new session
    starts with zero messages (transcript records are not copied).
    """
    source = await get_session(session_id)
    if source is None:
        return None
    sid = _uid()
    timestamp = now()
    fork_title = title if title is not None else f"Fork of {source.title}"
    await execute_commit(
        """INSERT INTO sessions (id, title, workspace, directory, model_provider, model_name, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (sid, fork_title, source.workspace, source.directory,
         source.model_provider, source.model_name, timestamp, timestamp),
    )
    return SessionInfo(
        id=sid, title=fork_title, workspace=source.workspace,
        directory=source.directory,
        model_provider=source.model_provider, model_name=source.model_name,
        created_at=timestamp, updated_at=timestamp,
    )


async def delete_session(session_id: str) -> None:
    dir_path = session_dir(session_id)
    if dir_path.exists():
        await asyncio.to_thread(shutil.rmtree, dir_path)
    await execute_commit(
        "DELETE FROM sessions WHERE id = ?", (session_id,)
    )
    await drop_session_lock(session_id)


# ── message persistence ─────────────────────────────────────────────────

async def save_message(msg: MessageRow) -> int:
    """Save a message row. Returns the auto-generated id."""
    row_id = await _next_message_id(msg.session_id)

    def _run(conn):
        conn.execute(
            "UPDATE sessions SET message_count = message_count + 1 WHERE id = ?",
            (msg.session_id,),
        )

    await write_transaction(_run)
    record = {
        "type": "message",
        "id": row_id,
        "role": msg.role,
        "content": msg.content,
        "content_format": msg.content_format,
        "created_at": msg.created_at,
    }
    if msg.tool_calls:
        record["tool_calls"] = msg.tool_calls
    if msg.tool_call_id:
        record["tool_call_id"] = msg.tool_call_id
    if msg.role == "tool" and message_status(msg.status) == "error":
        record["status"] = "error"
    await append_session_record(msg.session_id, "messages.jsonl", record)
    return row_id


async def _next_message_id(session_id: str) -> int:
    row = await fetch_one(
        "SELECT message_count FROM sessions WHERE id = ?",
        (session_id,),
    )
    next_from_count = int(row["message_count"] or 0) + 1 if row is not None else 1
    records = await read_session_records(session_id, "messages.jsonl") or []
    max_id = 0
    for record in records:
        message_id = record.get("id")
        if record.get("type") == "message" and isinstance(message_id, int):
            max_id = max(max_id, message_id)
    return max(next_from_count, max_id + 1)


async def load_messages(session_id: str) -> list[MessageRow]:
    """Load all messages for a session, ordered by id."""
    return await _load_messages_jsonl(session_id) or []


async def _load_messages_jsonl(session_id: str) -> list[MessageRow] | None:
    records = await read_session_records(session_id, "messages.jsonl")
    if records is None:
        return None

    messages: dict[int, MessageRow] = {}
    for record in records:
        rtype = record.get("type")
        if rtype == "session_cleared":
            messages.clear()
            continue
        if rtype == "message_deleted":
            mode = record.get("mode")
            if mode == "all":
                messages.clear()
            elif mode == "from":
                first_message_id = record.get("first_message_id")
                if isinstance(first_message_id, int):
                    for message_id in list(messages):
                        if message_id >= first_message_id:
                            del messages[message_id]
            elif mode == "through":
                last_message_id = record.get("last_message_id")
                if isinstance(last_message_id, int):
                    for message_id in list(messages):
                        if message_id <= last_message_id:
                            del messages[message_id]
            continue
        if rtype != "message":
            continue
        message_id = record.get("id")
        if not isinstance(message_id, int):
            continue
        messages[message_id] = MessageRow(
            id=message_id,
            session_id=session_id,
            role=str(record.get("role", "")),
            content=str(record.get("content", "")),
            content_format=str(record.get("content_format", "text") or "text"),
            tool_calls=record.get("tool_calls") if isinstance(record.get("tool_calls"), list) else None,
            tool_call_id=record.get("tool_call_id") if isinstance(record.get("tool_call_id"), str) else None,
            status=record.get("status") if isinstance(record.get("status"), str) else None,
            created_at=str(record.get("created_at", "")) or now(),
        )
    return [messages[key] for key in sorted(messages)]


async def count_messages(session_id: str) -> int:
    """Count persisted messages for a session."""
    row = await fetch_one(
        "SELECT message_count AS cnt FROM sessions WHERE id = ?",
        (session_id,),
    )
    return int(row["cnt"] or 0) if row else 0


async def _append_delete_cascade_records(
    session_id: str,
    *,
    mode: str,
    reason: str,
    first_message_id: int | None = None,
    last_message_id: int | None = None,
) -> None:
    created_at = now()
    context_record = {
        "type": "context_frame_deleted",
        "mode": mode,
        "reason": reason,
        "created_at": created_at,
    }
    runtime_record = {
        "type": "runtime_state_deleted",
        "mode": mode,
        "reason": reason,
        "created_at": created_at,
    }
    if first_message_id is not None:
        context_record["first_user_message_id"] = first_message_id
        runtime_record["first_message_id"] = first_message_id
    if last_message_id is not None:
        context_record["last_user_message_id"] = last_message_id
        runtime_record["last_message_id"] = last_message_id

    await append_session_record(session_id, "context/deletes.jsonl", context_record)
    await append_session_record(session_id, "runtime.jsonl", runtime_record)


async def clear_messages(session_id: str) -> None:
    previous_message_count = 0
    row = await fetch_one(
        "SELECT message_count FROM sessions WHERE id = ?",
        (session_id,),
    )
    previous_message_count = int(row["message_count"] or 0) if row else 0

    await append_session_record(session_id, "messages.jsonl", {
        "type": "session_cleared",
        "reason": "clear_messages",
        "cleared_at": now(),
        "previous_message_count": previous_message_count,
    })
    await _append_delete_cascade_records(
        session_id,
        mode="all",
        reason="clear_messages",
    )

    def _run(conn):
        conn.execute("DELETE FROM context_frames WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM session_runtime_state WHERE session_id = ?", (session_id,))
        conn.execute("UPDATE sessions SET message_count = 0 WHERE id = ?", (session_id,))

    await write_transaction(_run)


async def delete_messages_from(session_id: str, first_message_id: int) -> None:
    def _run(conn):
        conn.execute(
            "DELETE FROM context_frames WHERE session_id = ? AND user_message_id >= ?",
            (session_id, first_message_id),
        )

    await write_transaction(_run)
    await append_session_record(session_id, "messages.jsonl", {
        "type": "message_deleted",
        "mode": "from",
        "first_message_id": first_message_id,
        "reason": "delete_messages_from",
        "created_at": now(),
    })
    await _append_delete_cascade_records(
        session_id,
        mode="from",
        reason="delete_messages_from",
        first_message_id=first_message_id,
    )
    await _refresh_message_count_from_jsonl(session_id)
    await touch_session(session_id)


async def delete_messages_through(session_id: str, last_message_id: int) -> None:
    def _run(conn):
        conn.execute(
            "DELETE FROM context_frames WHERE session_id = ? AND user_message_id <= ?",
            (session_id, last_message_id),
        )

    await write_transaction(_run)
    await append_session_record(session_id, "messages.jsonl", {
        "type": "message_deleted",
        "mode": "through",
        "last_message_id": last_message_id,
        "reason": "delete_messages_through",
        "created_at": now(),
    })
    await _append_delete_cascade_records(
        session_id,
        mode="through",
        reason="delete_messages_through",
        last_message_id=last_message_id,
    )
    await _refresh_message_count_from_jsonl(session_id)
    await touch_session(session_id)


async def _refresh_message_count_from_jsonl(session_id: str) -> None:
    messages = await _load_messages_jsonl(session_id) or []
    await execute_commit(
        "UPDATE sessions SET message_count = ? WHERE id = ?",
        (len(messages), session_id),
    )


async def last_messages(session_id: str, n: int = 20) -> list[MessageRow]:
    """Last N messages for a session."""
    messages = await load_messages(session_id)
    return messages[-max(n, 0):] if n > 0 else []
