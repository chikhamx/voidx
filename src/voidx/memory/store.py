"""SQLite persistence layer — async-safe via asyncio.to_thread()."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TypeVar

DATA_DIR = Path.home() / ".voidx"

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
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        db_path = DATA_DIR / "voidx.db"
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        _init_schema(conn)
        _conn = conn
        return _conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT 'New session',
            workspace TEXT NOT NULL DEFAULT '.',
            model_provider TEXT NOT NULL DEFAULT 'anthropic',
            model_name TEXT NOT NULL DEFAULT 'claude-sonnet-4-6',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('system', 'user', 'assistant', 'tool')),
            content TEXT NOT NULL DEFAULT '',
            tool_calls TEXT,
            tool_call_id TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_messages_session
            ON messages(session_id, id);

        CREATE TABLE IF NOT EXISTS turns (
            session_id TEXT NOT NULL,
            turn_id INTEGER NOT NULL,
            user_message_id INTEGER,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            PRIMARY KEY (session_id, turn_id),
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (user_message_id) REFERENCES messages(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS transcript_nodes (
            session_id TEXT NOT NULL,
            turn_id INTEGER NOT NULL,
            node_id INTEGER NOT NULL,
            parent_node_id INTEGER,
            sort_order INTEGER NOT NULL,
            node_type TEXT NOT NULL,
            header TEXT NOT NULL DEFAULT '',
            body_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'running',
            collapsed INTEGER NOT NULL DEFAULT 0,
            elapsed REAL,
            message_id INTEGER,
            tool_call_id TEXT,
            agent_run_id TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (session_id, turn_id, node_id),
            FOREIGN KEY (session_id, turn_id) REFERENCES turns(session_id, turn_id) ON DELETE CASCADE,
            FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_transcript_nodes_session
            ON transcript_nodes(session_id, turn_id, sort_order);

        CREATE TABLE IF NOT EXISTS session_runtime_state (
            session_id TEXT PRIMARY KEY,
            interaction_mode TEXT NOT NULL DEFAULT 'auto',
            current_intent TEXT NOT NULL DEFAULT 'chat',
            previous_intent TEXT,
            current_goal TEXT NOT NULL DEFAULT '',
            awaiting_implementation_approval INTEGER NOT NULL DEFAULT 0,
            approved_scope TEXT NOT NULL DEFAULT '',
            pending_approval_json TEXT NOT NULL DEFAULT '',
            last_plan_summary TEXT NOT NULL DEFAULT '',
            recent_user_texts_json TEXT NOT NULL DEFAULT '[]',
            compaction_summary TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS session_task_runs (
            session_id TEXT PRIMARY KEY,
            goal TEXT NOT NULL DEFAULT '',
            phase TEXT NOT NULL DEFAULT 'clarify',
            status TEXT NOT NULL DEFAULT 'idle',
            approved_scope TEXT NOT NULL DEFAULT '',
            awaiting_implementation_approval INTEGER NOT NULL DEFAULT 0,
            pending_approval_json TEXT NOT NULL DEFAULT '',
            turn_count INTEGER NOT NULL DEFAULT 0,
            workflow_runs_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS message_runtime_snapshots (
            message_id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            interaction_mode TEXT NOT NULL DEFAULT 'auto',
            task_intent TEXT NOT NULL DEFAULT 'chat',
            implementation_allowed INTEGER NOT NULL DEFAULT 0,
            intent_resolution_reason TEXT NOT NULL DEFAULT '',
            goal TEXT NOT NULL DEFAULT '',
            goal_phase TEXT NOT NULL DEFAULT 'clarify',
            goal_status TEXT NOT NULL DEFAULT 'idle',
            goal_turn_count INTEGER NOT NULL DEFAULT 0,
            awaiting_implementation_approval INTEGER NOT NULL DEFAULT 0,
            approved_scope TEXT NOT NULL DEFAULT '',
            pending_approval_json TEXT NOT NULL DEFAULT '',
            intent_confidence REAL,
            intent_source TEXT NOT NULL DEFAULT '',
            intent_refined INTEGER NOT NULL DEFAULT 0,
            available_tool_ids_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_message_runtime_snapshots_session
            ON message_runtime_snapshots(session_id, message_id);

        CREATE TABLE IF NOT EXISTS context_frames (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            user_message_id INTEGER,
            frame_kind TEXT NOT NULL DEFAULT 'main',
            agent_role TEXT NOT NULL DEFAULT 'orchestrator',
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            prefix_hash TEXT NOT NULL,
            frame_hash TEXT NOT NULL,
            message_count INTEGER NOT NULL,
            token_estimate INTEGER NOT NULL DEFAULT 0,
            messages_json TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (user_message_id) REFERENCES messages(id) ON DELETE SET NULL
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
    # Migration: add content_format column for existing SQLite stores.
    try:
        conn.execute(
            "ALTER TABLE messages ADD COLUMN content_format TEXT NOT NULL DEFAULT 'text'"
        )
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            "ALTER TABLE session_runtime_state ADD COLUMN compaction_summary TEXT NOT NULL DEFAULT ''"
        )
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            "ALTER TABLE session_task_runs ADD COLUMN workflow_runs_json TEXT NOT NULL DEFAULT '{}'"
        )
    except sqlite3.OperationalError:
        pass
    for statement in (
        "ALTER TABLE session_runtime_state ADD COLUMN pending_approval_json TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE session_task_runs ADD COLUMN pending_approval_json TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE message_runtime_snapshots ADD COLUMN pending_approval_json TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE message_runtime_snapshots ADD COLUMN intent_confidence REAL",
        "ALTER TABLE message_runtime_snapshots ADD COLUMN intent_source TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE message_runtime_snapshots ADD COLUMN intent_refined INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE message_runtime_snapshots ADD COLUMN available_tool_ids_json TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE session_runtime_state ADD COLUMN recent_user_texts_json TEXT NOT NULL DEFAULT '[]'",
    ):
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass


async def _execute_commit(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    def _run():
        conn = _get_db()
        with _write_lock:
            cur = conn.execute(sql, params)
            conn.commit()
        return cur
    return await asyncio.to_thread(_run)


async def _fetch_all(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    def _run():
        conn = _get_db()
        return conn.execute(sql, params).fetchall()
    return await asyncio.to_thread(_run)


async def _fetch_one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    def _run():
        conn = _get_db()
        return conn.execute(sql, params).fetchone()
    return await asyncio.to_thread(_run)


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

    return await asyncio.to_thread(_run)
