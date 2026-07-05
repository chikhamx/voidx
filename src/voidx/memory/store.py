"""SQLite persistence layer — async-safe via asyncio.to_thread()."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TypeVar

from voidx.paths import voidx_home

DATA_DIR = voidx_home()
_SQLITE_TIMEOUT_SECONDS = 30.0
_SQLITE_BUSY_TIMEOUT_MS = 30000
_SQLITE_LOCK_MAX_ATTEMPTS = 4
_SQLITE_LOCK_RETRY_DELAY_SECONDS = 0.05
_SQLITE_LOCK_RETRY_MAX_DELAY_SECONDS = 0.5

_conn: sqlite3.Connection | None = None
_init_lock = threading.Lock()
_write_lock = threading.Lock()
T = TypeVar("T")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_db() -> sqlite3.Connection:
    global _conn
    with _init_lock:
        if _conn is not None:
            return _conn
        _conn = _run_with_locked_retry(_open_db)
        return _conn


def _prepare_db_path() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    store_dir = DATA_DIR / "store"
    store_dir.mkdir(parents=True, exist_ok=True)
    return store_dir / "voidx.db"


def _open_db() -> sqlite3.Connection:
    db_path = _prepare_db_path()
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=_SQLITE_TIMEOUT_SECONDS)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
        _init_schema(conn)
        return conn
    except Exception:
        conn.close()
        raise


def _is_database_locked(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and "database is locked" in str(exc).lower()


def _run_with_locked_retry(operation: Callable[[], T]) -> T:
    delay = _SQLITE_LOCK_RETRY_DELAY_SECONDS
    for attempt in range(1, _SQLITE_LOCK_MAX_ATTEMPTS + 1):
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if not _is_database_locked(exc) or attempt >= _SQLITE_LOCK_MAX_ATTEMPTS:
                raise
            time.sleep(delay)
            delay = min(delay * 2, _SQLITE_LOCK_RETRY_MAX_DELAY_SECONDS)
    raise RuntimeError("unreachable sqlite retry state")


_SCHEMA_VERSION = 1


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    row = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in row)


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    if not _column_exists(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _drop_column_if_exists(conn: sqlite3.Connection, table: str, column: str) -> None:
    if _column_exists(conn, table, column):
        conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")


def _migrate_to_v1(conn: sqlite3.Connection) -> None:
    """v0 → v1: add message_count/directory/workflow_route_json, drop legacy columns."""
    _add_column_if_missing(conn, "sessions", "message_count", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "sessions", "directory", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(
        conn, "session_runtime_state", "workflow_route_json", "TEXT NOT NULL DEFAULT ''"
    )
    _drop_column_if_exists(conn, "session_runtime_state", "pending_approval_json")
    _drop_column_if_exists(conn, "session_runtime_state", "recent_user_texts_json")


_MIGRATIONS = [_migrate_to_v1]


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT 'New session',
            workspace TEXT NOT NULL DEFAULT '.',
            directory TEXT NOT NULL DEFAULT '',
            model_provider TEXT NOT NULL DEFAULT 'anthropic',
            model_name TEXT NOT NULL DEFAULT 'claude-sonnet-4-6',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            message_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS session_runtime_state (
            session_id TEXT PRIMARY KEY,
            interaction_mode TEXT NOT NULL DEFAULT 'auto',
            current_intent TEXT NOT NULL DEFAULT 'coding',
            previous_intent TEXT,
            current_goal_json TEXT,
            workflow_route_json TEXT NOT NULL DEFAULT '',
            workflow_runs_json TEXT NOT NULL DEFAULT '{}',
            todo_state_json TEXT NOT NULL DEFAULT '',
            compaction_summary TEXT NOT NULL DEFAULT '',
            session_time TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS context_frames (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            user_message_id INTEGER,
            frame_kind TEXT NOT NULL DEFAULT 'main',
            agent_persona TEXT NOT NULL DEFAULT 'voidx',
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            prefix_hash TEXT NOT NULL,
            frame_hash TEXT NOT NULL,
            message_count INTEGER NOT NULL,
            token_estimate INTEGER NOT NULL DEFAULT 0,
            file_path TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_context_frames_session
            ON context_frames(session_id, id);

        CREATE INDEX IF NOT EXISTS idx_context_frames_prefix
            ON context_frames(session_id, prefix_hash);

        CREATE TABLE IF NOT EXISTS model_profiles (
            name TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            api_key TEXT NOT NULL DEFAULT '',
            base_url TEXT,
            protocol TEXT,
            reasoning_effort TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_model_profiles_provider
            ON model_profiles(provider);
    """)
    current_version = conn.execute("PRAGMA user_version").fetchone()[0]
    for version in range(current_version, _SCHEMA_VERSION):
        _MIGRATIONS[version](conn)
    if current_version < _SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
    _cleanup_legacy_payload_schema(conn)
    conn.commit()


def _cleanup_legacy_payload_schema(conn: sqlite3.Connection) -> None:
    if not any(_table_exists(conn, table) for table in (
        "messages",
        "turns",
        "transcript_nodes",
        "message_runtime_snapshots",
    )):
        return

    conn.commit()
    foreign_keys_enabled = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        for table in ("transcript_nodes", "turns", "message_runtime_snapshots", "messages"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys_enabled else 'OFF'}")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None





async def _execute_commit(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    def _run():
        conn = _get_db()
        with _write_lock:
            try:
                cur = conn.execute(sql, params)
                conn.commit()
                return cur
            except Exception:
                conn.rollback()
                raise
    return await asyncio.to_thread(lambda: _run_with_locked_retry(_run))


async def _fetch_all(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    def _run():
        conn = _get_db()
        return conn.execute(sql, params).fetchall()
    return await asyncio.to_thread(lambda: _run_with_locked_retry(_run))


async def _fetch_one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    def _run():
        conn = _get_db()
        return conn.execute(sql, params).fetchone()
    return await asyncio.to_thread(lambda: _run_with_locked_retry(_run))


async def _write_transaction(callback: Callable[[sqlite3.Connection], T]) -> T:
    def _run() -> T:
        conn = _get_db()
        with _write_lock:
            try:
                result = callback(conn)
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    return await asyncio.to_thread(lambda: _run_with_locked_retry(_run))
