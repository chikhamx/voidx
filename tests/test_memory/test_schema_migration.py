"""Tests for SQLite schema migration with PRAGMA user_version."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from voidx.memory.store import _init_schema, _get_db


class TestSchemaMigration:
    def test_fresh_db_sets_user_version(self, tmp_path: Path, monkeypatch) -> None:
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        _init_schema(conn)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version >= 1
        conn.close()

    def test_sessions_has_message_count(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        _init_schema(conn)
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
        assert "message_count" in cols
        conn.close()

    def test_sessions_has_directory(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        _init_schema(conn)
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
        assert "directory" in cols
        conn.close()

    def test_runtime_state_has_workflow_route(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        _init_schema(conn)
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(session_runtime_state)")}
        assert "workflow_route_json" in cols
        conn.close()

    def test_runtime_state_dropped_legacy_columns(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        _init_schema(conn)
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(session_runtime_state)")}
        assert "pending_approval_json" not in cols
        assert "recent_user_texts_json" not in cols
        conn.close()

    def test_idempotent_reinit(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        _init_schema(conn)
        version_after_first = conn.execute("PRAGMA user_version").fetchone()[0]
        _init_schema(conn)
        version_after_second = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version_after_first == version_after_second
        conn.close()

    def test_migrates_legacy_db_without_columns(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT 'New session',
                workspace TEXT NOT NULL DEFAULT '.',
                model_provider TEXT NOT NULL DEFAULT 'anthropic',
                model_name TEXT NOT NULL DEFAULT 'claude-sonnet-4-6',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE session_runtime_state (
                session_id TEXT PRIMARY KEY,
                interaction_mode TEXT NOT NULL DEFAULT 'auto',
                current_intent TEXT NOT NULL DEFAULT 'coding',
                previous_intent TEXT,
                current_goal_json TEXT,
                pending_approval_json TEXT,
                recent_user_texts_json TEXT,
                todo_state_json TEXT NOT NULL DEFAULT '',
                compaction_summary TEXT NOT NULL DEFAULT '',
                session_time TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            PRAGMA user_version = 0;
        """)
        conn.commit()
        _init_schema(conn)
        sess_cols = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
        rt_cols = {row["name"] for row in conn.execute("PRAGMA table_info(session_runtime_state)")}
        assert "message_count" in sess_cols
        assert "directory" in sess_cols
        assert "workflow_route_json" in rt_cols
        assert "pending_approval_json" not in rt_cols
        assert "recent_user_texts_json" not in rt_cols
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version >= 1
        conn.close()
