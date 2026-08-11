"""Durable lifecycle markers for sessions staged before publication."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from collections.abc import Collection
from typing import IO

from pydantic import BaseModel

from voidx.agent.adapters.persistence.session_repository import (
    SessionInfo,
    validate_runtime_profile,
)
from voidx.llm.domain.model import DEFAULT_MODEL
from voidx.persistence.jsonl import drop_session_lock, session_dir
from voidx.persistence.sqlite import fetch_all, fetch_one, now, write_transaction


def _owner_directory():
    from voidx.persistence import sqlite as store

    return store.DATA_DIR / "store" / "provisional-owners"


def _owner_path(owner_id: str):
    return _owner_directory() / f"{owner_id}.json"


def _owner_lock_path(owner_id: str):
    return _owner_directory() / f"{owner_id}.lock"


_OWNER_LEASES: dict[str, IO[bytes]] = {}


def _try_lock_owner(owner_id: str) -> IO[bytes] | None:
    import fcntl

    lock_path = _owner_lock_path(owner_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def _release_owner_lease(owner_id: str) -> None:
    import fcntl

    handle = _OWNER_LEASES.pop(owner_id, None)
    if handle is not None:
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
    _owner_lock_path(owner_id).unlink(missing_ok=True)


def register_provisional_owner(owner_id: str, *, pid: int | None = None) -> None:
    import json
    import os

    if not owner_id:
        raise ValueError("owner_id must not be empty")
    owns_current_process = pid is None or pid == os.getpid()
    acquired_lease = False
    if owns_current_process and owner_id not in _OWNER_LEASES:
        lease = _try_lock_owner(owner_id)
        if lease is None:
            raise RuntimeError(f"provisional owner already active: {owner_id}")
        _OWNER_LEASES[owner_id] = lease
        acquired_lease = True
    directory = _owner_directory()
    directory.mkdir(parents=True, exist_ok=True)
    path = _owner_path(owner_id)
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_text(
            json.dumps({"owner_id": owner_id, "pid": pid or os.getpid()}),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        if acquired_lease:
            _release_owner_lease(owner_id)
        raise


def active_provisional_owner_ids() -> set[str]:
    import json

    directory = _owner_directory()
    if not directory.exists():
        return set()
    active: set[str] = set()
    for path in directory.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            owner_id = str(payload["owner_id"])
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            path.unlink(missing_ok=True)
            continue
        probe = _try_lock_owner(owner_id)
        if probe is None:
            active.add(owner_id)
            continue
        probe.close()
        path.unlink(missing_ok=True)
        _owner_lock_path(owner_id).unlink(missing_ok=True)
    return active


class ProvisionalSessionMarker(BaseModel):
    session_id: str
    root_session_id: str
    owner_id: str
    created_at: str


def _marker_from_row(row: object) -> ProvisionalSessionMarker:
    return ProvisionalSessionMarker(
        session_id=row["session_id"],
        root_session_id=row["root_session_id"],
        owner_id=row["owner_id"],
        created_at=row["created_at"],
    )


async def get_provisional_session(session_id: str) -> ProvisionalSessionMarker | None:
    row = await fetch_one(
        "SELECT * FROM provisional_sessions WHERE session_id = ?",
        (session_id,),
    )
    return _marker_from_row(row) if row is not None else None


async def stage_provisional_session(
    *,
    owner_id: str,
    session_id: str | None = None,
    root_session_id: str | None = None,
    workspace: str = ".",
    provider: str = "anthropic",
    model: str = DEFAULT_MODEL,
    title: str = "New session",
    directory: str = "",
    profile: str = "coding",
) -> SessionInfo:
    """Create a session and its provisional marker in one transaction."""
    from voidx.agent.adapters.persistence.session_repository import _uid

    if not owner_id:
        raise ValueError("owner_id must not be empty")
    profile = validate_runtime_profile(profile)
    sid = session_id or _uid()
    timestamp = now()

    def _stage(conn):
        existing = conn.execute(
            """SELECT sessions.*, provisional_sessions.owner_id
               FROM sessions
               JOIN provisional_sessions
                 ON provisional_sessions.session_id = sessions.id
               WHERE sessions.id = ?""",
            (sid,),
        ).fetchone()
        if existing is not None:
            if existing["owner_id"] != owner_id:
                raise ValueError(f"provisional session already owned: {sid}")
            return existing
        root_id = sid
        if root_session_id is not None:
            root = conn.execute(
                "SELECT root_session_id FROM provisional_sessions WHERE session_id = ?",
                (root_session_id,),
            ).fetchone()
            if root is None:
                raise ValueError(f"provisional root session not found: {root_session_id}")
            root_id = root["root_session_id"]
        conn.execute(
            """INSERT INTO sessions
               (id, title, workspace, directory, model_provider, model_name,
                runtime_profile, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (sid, title, workspace, directory, provider, model, profile, timestamp, timestamp),
        )
        conn.execute(
            """INSERT INTO provisional_sessions
               (session_id, root_session_id, owner_id, created_at)
               VALUES (?, ?, ?, ?)""",
            (sid, root_id, owner_id, timestamp),
        )

    existing = await write_transaction(_stage)
    if existing is not None:
        return SessionInfo(
            id=existing["id"],
            title=existing["title"],
            workspace=existing["workspace"],
            directory=existing["directory"],
            model_provider=existing["model_provider"],
            model_name=existing["model_name"],
            runtime_profile=existing["runtime_profile"],
            created_at=existing["created_at"],
            updated_at=existing["updated_at"],
        )
    return SessionInfo(
        id=sid,
        title=title,
        workspace=workspace,
        directory=directory,
        model_provider=provider,
        model_name=model,
        runtime_profile=profile,
        created_at=timestamp,
        updated_at=timestamp,
    )


async def promote_provisional_session(session_id: str) -> int:
    """Publish the entire provisional group containing ``session_id``."""
    def _promote(conn) -> int:
        marker = conn.execute(
            "SELECT root_session_id FROM provisional_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if marker is None:
            return 0
        cursor = conn.execute(
            "DELETE FROM provisional_sessions WHERE root_session_id = ?",
            (marker["root_session_id"],),
        )
        return cursor.rowcount

    return await write_transaction(_promote)


async def rollback_provisional_session(session_id: str) -> int:
    """Delete every session and marker in the provisional root group."""
    def _rollback(conn) -> list[str]:
        marker = conn.execute(
            "SELECT root_session_id FROM provisional_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if marker is None:
            return []
        rows = conn.execute(
            "SELECT session_id FROM provisional_sessions WHERE root_session_id = ?",
            (marker["root_session_id"],),
        ).fetchall()
        session_ids = [row["session_id"] for row in rows]
        conn.executemany(
            "DELETE FROM agent_threads WHERE session_id = ?",
            ((sid,) for sid in session_ids),
        )
        conn.executemany("DELETE FROM sessions WHERE id = ?", ((sid,) for sid in session_ids))
        return session_ids

    session_ids = await write_transaction(_rollback)
    for sid in session_ids:
        path = session_dir(sid)
        if path.exists():
            await asyncio.to_thread(shutil.rmtree, path)
        await drop_session_lock(sid)
    return len(session_ids)


async def find_orphaned_provisional_roots(
    *,
    active_owner_ids: Collection[str],
    created_before: str,
) -> list[str]:
    """Return old roots whose entire group has no currently active owner.

    This function only discovers candidates. Callers must supply owner liveness and
    explicitly choose whether to roll candidates back.
    """
    if not created_before:
        raise ValueError("created_before must not be empty")
    active_owners = tuple(sorted(set(active_owner_ids)))
    params: tuple[object, ...] = (created_before,)
    active_clause = ""
    if active_owners:
        placeholders = ", ".join("?" for _ in active_owners)
        active_clause = f"""
            AND NOT EXISTS (
                SELECT 1
                FROM provisional_sessions AS active
                WHERE active.root_session_id = marker.root_session_id
                  AND active.owner_id IN ({placeholders})
            )
        """
        params += active_owners
    rows = await fetch_all(
        f"""SELECT marker.root_session_id
            FROM provisional_sessions AS marker
            GROUP BY marker.root_session_id
            HAVING MAX(marker.created_at) < ?
            {active_clause}
            ORDER BY marker.root_session_id""",
        params,
    )
    return [row["root_session_id"] for row in rows]


async def cleanup_orphaned_provisional_sessions(
    *,
    active_owner_ids: Collection[str],
    created_before: str,
    dry_run: bool = True,
) -> list[str]:
    """Discover orphan roots and optionally roll them back.

    Cleanup is deliberately dry-run by default. Owner liveness remains the
    caller's responsibility so this layer cannot guess across processes.
    """
    roots = await find_orphaned_provisional_roots(
        active_owner_ids=active_owner_ids,
        created_before=created_before,
    )
    if dry_run:
        return roots
    for root_session_id in roots:
        await rollback_provisional_session(root_session_id)
    return roots


async def cleanup_dead_provisional_owners() -> list[str]:
    active_owner_ids = tuple(sorted(active_provisional_owner_ids()))
    if active_owner_ids:
        placeholders = ", ".join("?" for _ in active_owner_ids)
        rows = await fetch_all(
            f"""SELECT DISTINCT marker.root_session_id
                FROM provisional_sessions AS marker
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM provisional_sessions AS active
                    WHERE active.root_session_id = marker.root_session_id
                      AND active.owner_id IN ({placeholders})
                )
                ORDER BY marker.root_session_id""",
            active_owner_ids,
        )
    else:
        rows = await fetch_all(
            "SELECT DISTINCT root_session_id FROM provisional_sessions ORDER BY root_session_id"
        )
    roots = [row["root_session_id"] for row in rows]
    for root_session_id in roots:
        await rollback_provisional_session(root_session_id)
    return roots


async def close_provisional_owner(owner_id: str) -> int:
    """Release one owner and delete only groups left without any owner marker."""
    def _close(conn) -> list[str]:
        rows = conn.execute(
            """SELECT root_session_id, session_id
               FROM provisional_sessions
               WHERE root_session_id IN (
                   SELECT root_session_id FROM provisional_sessions WHERE owner_id = ?
               )
               ORDER BY root_session_id, session_id""",
            (owner_id,),
        ).fetchall()
        roots = sorted({row["root_session_id"] for row in rows})
        grouped_session_ids = {
            root_id: [row["session_id"] for row in rows if row["root_session_id"] == root_id]
            for root_id in roots
        }
        deleted_session_ids: list[str] = []
        for root_id in roots:
            survivor = conn.execute(
                """SELECT owner_id
                   FROM provisional_sessions
                   WHERE root_session_id = ? AND owner_id <> ?
                   ORDER BY owner_id
                   LIMIT 1""",
                (root_id, owner_id),
            ).fetchone()
            if survivor is not None:
                conn.execute(
                    """UPDATE provisional_sessions
                       SET owner_id = ?
                       WHERE root_session_id = ? AND owner_id = ?""",
                    (survivor["owner_id"], root_id, owner_id),
                )
                continue
            session_ids = grouped_session_ids[root_id]
            conn.execute(
                "DELETE FROM provisional_sessions WHERE root_session_id = ?",
                (root_id,),
            )
            conn.executemany(
                "DELETE FROM agent_threads WHERE session_id = ?",
                ((sid,) for sid in session_ids),
            )
            conn.executemany("DELETE FROM sessions WHERE id = ?", ((sid,) for sid in session_ids))
            deleted_session_ids.extend(session_ids)
        return deleted_session_ids

    try:
        session_ids = await write_transaction(_close)
        for sid in session_ids:
            path = session_dir(sid)
            if path.exists():
                await asyncio.to_thread(shutil.rmtree, path)
            await drop_session_lock(sid)
        return len(session_ids)
    finally:
        _owner_path(owner_id).unlink(missing_ok=True)
        _release_owner_lease(owner_id)
