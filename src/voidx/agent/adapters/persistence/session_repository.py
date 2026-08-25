"""Session manager — CRUD, message persistence, auto-titling.

Inspired by opencode's session system: typed Info, create/fork/remove,
timestamp-based listing, message hydration.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import time
import uuid

from typing import Any

from pydantic import BaseModel, Field

from voidx.agent.domain.agent_profile import AgentProfileSnapshot
from voidx.agent.adapters.persistence.session_models import (
    SessionInfo,
    snapshot_columns as _snapshot_columns,
    snapshot_from_row as _snapshot_from_row,
    validate_runtime_profile,
)
from voidx.agent.ports.persistence import GoalRuntimeCorruption
from voidx.llm.domain.model import DEFAULT_MODEL
from voidx.llm.message_status import message_status
from voidx.persistence.jsonl import (
    append_session_bytes,
    append_session_record,
    delete_session_file,
    drop_session_lock,
    encode_jsonl_record,
    read_session_bytes,
    read_session_records,
    session_dir,
    session_directory_locks,
    truncate_session_file,
)
from voidx.persistence.session_ids import validate_session_storage_id
from voidx.persistence.sqlite import execute_commit, fetch_all, fetch_one, now, write_transaction


def _uid() -> str:
    return uuid.uuid4().hex[:12]


# ── models ──────────────────────────────────────────────────────────────

RUNTIME_PROFILES = ("coding", "chat", "loop", "goal")


def _pin_profile_snapshot(
    workspace: str,
    profile: str,
    snapshot: AgentProfileSnapshot | None,
) -> AgentProfileSnapshot | None:
    """Resolve a snapshot when the caller did not pin one."""
    if snapshot is not None:
        return snapshot
    from voidx.agent.application.agent_registry import agent_registry_for

    try:
        return agent_registry_for(workspace or ".").resolve(profile).snapshot
    except Exception:
        return None


class MessageRow(BaseModel):
    id: int | None = None  # auto-increment
    session_id: str
    role: str  # system | user | assistant | tool
    content: str = ""
    content_format: str = "text"  # "text" | "structured" (e.g. DeepSeek thinking blocks)
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None
    status: str | None = None
    additional_kwargs: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=now)



class GoalTranscriptRecord(BaseModel):
    session_id: str
    generation: str
    attempt_id: str
    attempt_number: int
    local_sequence: int
    session_sequence: int
    fencing_token: int
    filename: str
    start_offset: int
    end_offset: int
    payload_hash: str
    accepted_at: str


class GoalTranscriptCorruption(GoalRuntimeCorruption):
    """An accepted transcript index no longer matches its canonical bytes."""


def _goal_transcript_record(row: Any) -> GoalTranscriptRecord:
    return GoalTranscriptRecord(**dict(row))


def _canonical_message_payload(message: dict[str, Any], session_sequence: int) -> bytes:
    if not isinstance(message, dict):
        raise ValueError("Goal transcript message must be an object")
    role = message.get("role")
    if role not in {"system", "user", "assistant", "tool"}:
        raise ValueError("Goal transcript message role is invalid")
    record = dict(message)
    record["type"] = "message"
    record["id"] = session_sequence
    return encode_jsonl_record(record)


def _validate_goal_transcript_binding(conn: Any, session_id: str, generation: str) -> Any:
    binding = conn.execute(
        """SELECT * FROM goal_generations
           WHERE generation = ? AND visibility = 'internal'
             AND (work_session_id = ? OR evaluator_session_id = ?)""",
        (generation, session_id, session_id),
    ).fetchone()
    if binding is None:
        raise ValueError("Goal transcript session/generation binding is invalid")
    cleanup = conn.execute(
        "SELECT status FROM goal_generation_cleanup WHERE generation = ?",
        (generation,),
    ).fetchone()
    if cleanup is not None:
        raise ValueError("Goal transcript generation cleanup blocks writes")
    return binding


def _validate_goal_transcript_attempt(
    conn: Any,
    *,
    binding: Any,
    session_id: str,
    generation: str,
    attempt_id: str,
    attempt_number: int,
    lease_owner: str,
    fencing_token: int,
) -> None:
    attempt = conn.execute(
        "SELECT * FROM runtime_turn_attempts WHERE id = ?",
        (attempt_id,),
    ).fetchone()
    source = (
        conn.execute(
            "SELECT * FROM runtime_outbox WHERE id = ?",
            (attempt["source_outbox_id"],),
        ).fetchone()
        if attempt is not None
        else None
    )
    expected_phase = (
        "work" if session_id == binding["work_session_id"] else "evaluator"
    )
    frame = json.loads(attempt["input_frame_json"] or "{}") if attempt is not None else {}
    now_ts = time.time()
    if (
        not lease_owner
        or fencing_token < 1
        or attempt is None
        or attempt["thread_id"] != binding["goal_thread_id"]
        or attempt["status"] != "prepared"
        or not bool(attempt["side_effect_started"])
        or attempt["lease_owner"] != lease_owner
        or int(attempt["fencing_token"]) != int(fencing_token)
        or float(attempt["lease_expires_at"]) <= now_ts
        or source is None
        or source["kind"] != "goal_prompt"
        or source["delivered_at"] is not None
        or source["claimed_by"] != lease_owner
        or float(source["claimed_until"] or 0) <= now_ts
        or frame.get("generation") != generation
        or frame.get("phase") != expected_phase
        or int(frame.get("attempt_number", -1)) != attempt_number
    ):
        raise ValueError("Goal transcript attempt lease conflict")


async def append_goal_transcript_message(
    *,
    session_id: str,
    generation: str,
    attempt_id: str,
    attempt_number: int,
    local_sequence: int,
    lease_owner: str,
    fencing_token: int,
    message: dict[str, Any],
) -> GoalTranscriptRecord:
    """Append and accept one fenced canonical Goal child-session message."""
    validate_session_storage_id(session_id)
    if not generation or not attempt_id or not lease_owner:
        raise ValueError("Goal transcript generation and attempt lease are required")
    if attempt_number < 0 or local_sequence < 1 or fencing_token < 1:
        raise ValueError("Goal transcript sequence and fencing values must be positive")
    filename = "messages.jsonl"

    async with session_directory_locks((session_id,)):
        def _prepare(conn: Any) -> tuple[GoalTranscriptRecord | None, int]:
            binding = _validate_goal_transcript_binding(conn, session_id, generation)
            _validate_goal_transcript_attempt(
                conn,
                binding=binding,
                session_id=session_id,
                generation=generation,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                lease_owner=lease_owner,
                fencing_token=fencing_token,
            )
            existing = conn.execute(
                """SELECT * FROM goal_transcript_records
                   WHERE session_id = ? AND attempt_id = ? AND local_sequence = ?""",
                (session_id, attempt_id, local_sequence),
            ).fetchone()
            if existing is not None:
                accepted = _goal_transcript_record(existing)
                expected = _canonical_message_payload(message, accepted.session_sequence)
                expected_hash = hashlib.sha256(expected[:-1]).hexdigest()
                if (
                    accepted.generation != generation
                    or accepted.attempt_number != attempt_number
                    or accepted.fencing_token != fencing_token
                    or accepted.filename != filename
                    or accepted.payload_hash != expected_hash
                ):
                    raise ValueError("Goal transcript idempotency conflict")
                return accepted, accepted.session_sequence
            row = conn.execute(
                """SELECT COALESCE(MAX(session_sequence), 0) + 1
                   FROM goal_transcript_records WHERE session_id = ?""",
                (session_id,),
            ).fetchone()
            return None, int(row[0])

        existing, session_sequence = await write_transaction(_prepare)
        if existing is not None:
            return existing

        payload = _canonical_message_payload(message, session_sequence)
        payload_hash = hashlib.sha256(payload[:-1]).hexdigest()
        start_offset, end_offset = await append_session_bytes(
            session_id,
            filename,
            payload,
        )
        accepted_at = now()

        def _accept(conn: Any) -> GoalTranscriptRecord:
            binding = _validate_goal_transcript_binding(conn, session_id, generation)
            _validate_goal_transcript_attempt(
                conn,
                binding=binding,
                session_id=session_id,
                generation=generation,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                lease_owner=lease_owner,
                fencing_token=fencing_token,
            )
            conflict = conn.execute(
                """SELECT * FROM goal_transcript_records
                   WHERE session_id = ? AND attempt_id = ? AND local_sequence = ?""",
                (session_id, attempt_id, local_sequence),
            ).fetchone()
            if conflict is not None:
                raise ValueError("Goal transcript idempotency conflict")
            next_row = conn.execute(
                """SELECT COALESCE(MAX(session_sequence), 0) + 1
                   FROM goal_transcript_records WHERE session_id = ?""",
                (session_id,),
            ).fetchone()
            if int(next_row[0]) != session_sequence:
                raise ValueError("Goal transcript session sequence conflict")
            conn.execute(
                """INSERT INTO goal_transcript_records (
                       session_id, generation, attempt_id, attempt_number,
                       local_sequence, session_sequence, fencing_token, filename,
                       start_offset, end_offset, payload_hash, accepted_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    generation,
                    attempt_id,
                    attempt_number,
                    local_sequence,
                    session_sequence,
                    fencing_token,
                    filename,
                    start_offset,
                    end_offset,
                    payload_hash,
                    accepted_at,
                ),
            )
            updated = conn.execute(
                """UPDATE sessions SET message_count = message_count + 1,
                       updated_at = ? WHERE id = ?""",
                (accepted_at, session_id),
            )
            if updated.rowcount != 1:
                raise ValueError("Goal transcript session does not exist")
            row = conn.execute(
                """SELECT * FROM goal_transcript_records
                   WHERE session_id = ? AND session_sequence = ?""",
                (session_id, session_sequence),
            ).fetchone()
            return _goal_transcript_record(row)

        try:
            return await write_transaction(_accept)
        except Exception:
            await truncate_session_file(session_id, filename, start_offset)
            raise


async def load_goal_transcript_messages(session_id: str) -> list[MessageRow]:
    """Hydrate only SQLite-accepted Goal transcript byte ranges."""
    validate_session_storage_id(session_id)
    rows = await fetch_all(
        """SELECT * FROM goal_transcript_records
           WHERE session_id = ? ORDER BY session_sequence""",
        (session_id,),
    )
    count_row = await fetch_one(
        "SELECT message_count FROM sessions WHERE id = ?",
        (session_id,),
    )
    if count_row is None or int(count_row["message_count"] or 0) != len(rows):
        raise GoalTranscriptCorruption("canonical transcript message count mismatch")

    messages: list[MessageRow] = []
    previous_end = 0
    for expected_sequence, row in enumerate(rows, start=1):
        accepted = _goal_transcript_record(row)
        if (
            accepted.session_sequence != expected_sequence
            or accepted.filename != "messages.jsonl"
            or accepted.start_offset < previous_end
            or accepted.end_offset <= accepted.start_offset
        ):
            raise GoalTranscriptCorruption("canonical transcript offset/order corruption")
        payload = await read_session_bytes(
            session_id,
            accepted.filename,
            accepted.start_offset,
            accepted.end_offset,
        )
        if (
            payload is None
            or not payload.endswith(b"\n")
            or payload.endswith(b"\n\n")
            or hashlib.sha256(payload[:-1]).hexdigest() != accepted.payload_hash
        ):
            raise GoalTranscriptCorruption("canonical transcript hash/offset corruption")
        try:
            record = json.loads(payload[:-1].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GoalTranscriptCorruption("canonical transcript payload corruption") from exc
        if (
            not isinstance(record, dict)
            or record.get("type") != "message"
            or record.get("id") != accepted.session_sequence
            or not isinstance(record.get("role"), str)
        ):
            raise GoalTranscriptCorruption("canonical transcript record corruption")
        messages.append(_message_row_from_record(session_id, record))
        previous_end = accepted.end_offset
    return messages


# ── session CRUD ────────────────────────────────────────────────────────

async def create_session(
    workspace: str = ".",
    provider: str = "anthropic",
    model: str = DEFAULT_MODEL,
    *,
    title: str = "New session",
    directory: str = "",
    profile: str = "coding",
    session_id: str | None = None,
    profile_snapshot: AgentProfileSnapshot | None = None,
) -> SessionInfo:
    profile = validate_runtime_profile(profile)
    sid = validate_session_storage_id(session_id or _uid())
    timestamp = now()
    profile_snapshot = _pin_profile_snapshot(workspace, profile, profile_snapshot)
    revision, content_hash, snapshot_hash, source, payload_json = _snapshot_columns(profile_snapshot)
    await execute_commit(
        """INSERT INTO sessions (id, title, workspace, directory, model_provider, model_name, runtime_profile,
               runtime_profile_revision, runtime_profile_content_hash, runtime_profile_hash,
               runtime_profile_source, runtime_profile_snapshot, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (sid, title, workspace, directory, provider, model, profile,
         revision, content_hash, snapshot_hash, source, payload_json, timestamp, timestamp),
    )
    return SessionInfo(
        id=sid, title=title, workspace=workspace, directory=directory,
        model_provider=provider, model_name=model, runtime_profile=profile,
        created_at=timestamp, updated_at=timestamp, profile_snapshot=profile_snapshot,
    )


async def ensure_session(
    session_id: str,
    workspace: str,
    *,
    profile: str = "coding",
    title: str = "Loop session",
    root_session_id: str | None = None,
    profile_snapshot: AgentProfileSnapshot | None = None,
) -> None:
    """Insert a session and inherit its provisional root when applicable."""
    session_id = validate_session_storage_id(session_id)
    if root_session_id is not None:
        root_session_id = validate_session_storage_id(root_session_id)
    profile = validate_runtime_profile(profile)
    timestamp = now()
    profile_snapshot = _pin_profile_snapshot(workspace, profile, profile_snapshot)
    revision, content_hash, snapshot_hash, source, payload_json = _snapshot_columns(profile_snapshot)

    def _ensure(conn) -> None:
        conn.execute(
            """INSERT OR IGNORE INTO sessions (id, title, workspace, directory, model_provider, model_name, runtime_profile,
                       runtime_profile_revision, runtime_profile_content_hash, runtime_profile_hash,
                       runtime_profile_source, runtime_profile_snapshot, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, title, workspace, workspace, "anthropic", DEFAULT_MODEL, profile,
             revision, content_hash, snapshot_hash, source, payload_json, timestamp, timestamp),
        )
        if not root_session_id:
            return
        root = conn.execute(
            "SELECT root_session_id, owner_id FROM provisional_sessions WHERE session_id = ?",
            (root_session_id,),
        ).fetchone()
        if root is not None:
            conn.execute(
                """INSERT OR IGNORE INTO provisional_sessions
                   (session_id, root_session_id, owner_id, created_at)
                   VALUES (?, ?, ?, ?)""",
                (session_id, root["root_session_id"], root["owner_id"], timestamp),
            )

    await write_transaction(_ensure)


async def get_session(session_id: str) -> SessionInfo | None:
    session_id = validate_session_storage_id(session_id)
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
        profile_snapshot=_snapshot_from_row(row),
    )


async def list_sessions(limit: int = 50) -> list[SessionInfo]:
    rows = await fetch_all(
        """SELECT *
           FROM sessions AS s
           WHERE NOT EXISTS (
               SELECT 1 FROM provisional_sessions AS p WHERE p.session_id = s.id
           )
             AND NOT EXISTS (
               SELECT 1 FROM goal_generations AS g
               WHERE g.evaluator_session_id = s.id
                  OR g.work_session_id = s.id
           )
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
            profile_snapshot=_snapshot_from_row(row),
        )
        for row in rows
    ]


async def latest_session_for_workspace(workspace: str) -> SessionInfo | None:
    row = await fetch_one(
        """SELECT *
           FROM sessions
           WHERE workspace = ?
             AND NOT EXISTS (
                 SELECT 1 FROM provisional_sessions AS p WHERE p.session_id = sessions.id
             )
             AND NOT EXISTS (
                 SELECT 1 FROM goal_generations AS g
                 WHERE g.evaluator_session_id = sessions.id
                    OR g.work_session_id = sessions.id
             )
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
        profile_snapshot=_snapshot_from_row(row),
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


async def update_session_profile(
    session_id: str,
    profile: str,
    *,
    profile_snapshot: AgentProfileSnapshot | None = None,
) -> None:
    profile = validate_runtime_profile(profile)
    if profile_snapshot is None:
        existing = await fetch_one("SELECT workspace FROM sessions WHERE id = ?", (session_id,))
        workspace = existing["workspace"] if existing is not None else "."
        profile_snapshot = _pin_profile_snapshot(workspace, profile, None)
    revision, content_hash, snapshot_hash, source, payload_json = _snapshot_columns(profile_snapshot)
    await execute_commit(
        """UPDATE sessions SET runtime_profile = ?, runtime_profile_revision = ?,
               runtime_profile_content_hash = ?, runtime_profile_hash = ?,
               runtime_profile_source = ?, runtime_profile_snapshot = ?, updated_at = ?
           WHERE id = ?""",
        (profile, revision, content_hash, snapshot_hash, source, payload_json, now(), session_id),
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
    revision, content_hash, snapshot_hash, source_scope, payload_json = _snapshot_columns(
        source.profile_snapshot
    )
    await execute_commit(
        """INSERT INTO sessions (id, title, workspace, directory, model_provider, model_name, runtime_profile,
               runtime_profile_revision, runtime_profile_content_hash, runtime_profile_hash,
               runtime_profile_source, runtime_profile_snapshot, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (sid, fork_title, source.workspace, source.directory,
         source.model_provider, source.model_name, source.runtime_profile,
         revision, content_hash, snapshot_hash, source_scope, payload_json, timestamp, timestamp),
    )
    return SessionInfo(
        id=sid, title=fork_title, workspace=source.workspace,
        directory=source.directory,
        model_provider=source.model_provider, model_name=source.model_name,
        runtime_profile=source.runtime_profile,
        created_at=timestamp, updated_at=timestamp,
        profile_snapshot=source.profile_snapshot,
    )


async def delete_session(session_id: str) -> None:
    session_id = validate_session_storage_id(session_id)
    from voidx.agent.adapters.persistence.thread_repository import ThreadStore
    from voidx.agent.application.automation.goal.cleanup import GoalCleanupCoordinator

    await GoalCleanupCoordinator(
        store=ThreadStore(),
        delete_main_session=_delete_session_without_goal_cleanup,
    ).delete_main_session(session_id)


async def _delete_session_without_goal_cleanup(session_id: str) -> None:
    session_id = validate_session_storage_id(session_id)
    async with session_directory_locks((session_id,)):
        internal = await fetch_one(
            """SELECT generation FROM goal_generations
               WHERE evaluator_session_id = ? OR work_session_id = ?""",
            (session_id, session_id),
        )
        if internal is not None:
            raise ValueError("cannot delete an internal Goal session directly")
        dir_path = session_dir(session_id)
        if dir_path.exists():
            await asyncio.to_thread(shutil.rmtree, dir_path)
        await execute_commit(
            "DELETE FROM sessions WHERE id = ?", (session_id,)
        )
    await drop_session_lock(session_id)


# ── message persistence ─────────────────────────────────────────────────

async def save_message(msg: MessageRow) -> int:
    """Save a message row outside Goal internal transcript storage."""
    async with session_directory_locks((msg.session_id,)):
        internal = await fetch_one(
            """SELECT 1 FROM goal_generations
               WHERE work_session_id = ? OR evaluator_session_id = ?""",
            (msg.session_id, msg.session_id),
        )
        if internal is not None:
            raise ValueError("Goal internal sessions require the accepted transcript writer")
        row_id = await _next_message_id(msg.session_id)

        def _accept(conn):
            conn.execute(
                "UPDATE sessions SET message_count = message_count + 1 WHERE id = ?",
                (msg.session_id,),
            )

        await write_transaction(_accept)
        record = {
            "type": "message",
            "id": row_id,
            "role": msg.role,
            "content": msg.content,
            "content_format": msg.content_format,
            "created_at": msg.created_at,
        }
        if msg.additional_kwargs:
            record["additional_kwargs"] = msg.additional_kwargs
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
    """Load all messages, using accepted offsets for Goal child sessions."""
    session_id = validate_session_storage_id(session_id)
    binding = await fetch_one(
        """SELECT 1 FROM goal_generations
           WHERE work_session_id = ? OR evaluator_session_id = ?""",
        (session_id, session_id),
    )
    if binding is not None:
        return await load_goal_transcript_messages(session_id)
    return await _load_messages_jsonl(session_id) or []



def _message_row_from_record(session_id: str, record: dict[str, Any]) -> MessageRow:
    message_id = record.get("id")
    if not isinstance(message_id, int):
        raise ValueError("message record id must be an integer")
    return MessageRow(
        id=message_id,
        session_id=session_id,
        role=str(record.get("role", "")),
        content=str(record.get("content", "")),
        content_format=str(record.get("content_format", "text") or "text"),
        tool_calls=(
            record.get("tool_calls")
            if isinstance(record.get("tool_calls"), list)
            else None
        ),
        tool_call_id=(
            record.get("tool_call_id")
            if isinstance(record.get("tool_call_id"), str)
            else None
        ),
        status=record.get("status") if isinstance(record.get("status"), str) else None,
        additional_kwargs=(
            record.get("additional_kwargs")
            if isinstance(record.get("additional_kwargs"), dict)
            else {}
        ),
        created_at=str(record.get("created_at", "")) or now(),
    )


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
        messages[message_id] = _message_row_from_record(session_id, record)
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
    await _unlink_context_frame_files(
        session_id,
        await fetch_all(
            "SELECT id, file_path FROM context_frames WHERE session_id = ?",
            (session_id,),
        ),
    )

    def _run(conn):
        conn.execute("DELETE FROM context_frames WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM session_runtime_state WHERE session_id = ?", (session_id,))
        conn.execute("UPDATE sessions SET message_count = 0 WHERE id = ?", (session_id,))

    await write_transaction(_run)


async def delete_messages_from(session_id: str, first_message_id: int) -> None:
    await _unlink_context_frame_files(
        session_id,
        await fetch_all(
            "SELECT id, file_path FROM context_frames WHERE session_id = ? AND user_message_id >= ?",
            (session_id, first_message_id),
        ),
    )

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
    await _unlink_context_frame_files(
        session_id,
        await fetch_all(
            "SELECT id, file_path FROM context_frames WHERE session_id = ? AND user_message_id <= ?",
            (session_id, last_message_id),
        ),
    )

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


async def _unlink_context_frame_files(session_id: str, rows: list[Any]) -> None:
    for row in rows:
        file_path = str(row["file_path"] or f"context/{row['id']}.jsonl")
        try:
            await delete_session_file(session_id, file_path)
        except (ValueError, OSError):
            continue


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
