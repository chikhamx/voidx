"""SQLite persistence layer — async-safe via asyncio.to_thread()."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path

DATA_DIR = Path.home() / ".voidx"

_conn: sqlite3.Connection | None = None
_init_lock = threading.Lock()
_write_lock = threading.Lock()


def _get_db() -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    with _init_lock:
        if _conn is not None:
            return _conn
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        db_path = DATA_DIR / "voidx.db"
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _init_schema(conn)
        _conn = conn
        return _conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT 'New session',
            workspace TEXT NOT NULL DEFAULT '.',
            model_provider TEXT NOT NULL DEFAULT 'deepseek',
            model_name TEXT NOT NULL DEFAULT 'deepseek-v4-pro',
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
    """)


async def _execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    def _run():
        conn = _get_db()
        return conn.execute(sql, params)
    return await asyncio.to_thread(_run)


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
