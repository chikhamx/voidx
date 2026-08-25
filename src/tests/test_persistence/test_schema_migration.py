"""Tests for SQLite schema migration with PRAGMA user_version."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from voidx.bootstrap.persistence import migrate_connection
from voidx.persistence.sqlite import _get_db


class TestSchemaMigration:
    def test_fresh_db_sets_user_version(self, tmp_path: Path, monkeypatch) -> None:
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        migrate_connection(conn)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version >= 1
        conn.close()

    def test_sessions_has_message_count(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        migrate_connection(conn)
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
        assert "message_count" in cols
        conn.close()

    def test_sessions_has_directory(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        migrate_connection(conn)
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
        assert "directory" in cols
        conn.close()

    def test_runtime_state_has_workflow_route(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        migrate_connection(conn)
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(session_runtime_state)")}
        assert "workflow_route_json" in cols
        conn.close()

    def test_runtime_state_dropped_legacy_columns(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        migrate_connection(conn)
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(session_runtime_state)")}
        assert "pending_approval_json" not in cols
        assert "recent_user_texts_json" not in cols
        conn.close()

    def test_idempotent_reinit(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        migrate_connection(conn)
        version_after_first = conn.execute("PRAGMA user_version").fetchone()[0]
        migrate_connection(conn)
        version_after_second = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version_after_first == version_after_second
        conn.close()

    def test_agent_runtime_thread_tables_exist(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        migrate_connection(conn)
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "agent_threads" in tables
        assert "agent_thread_state" in tables
        assert "runtime_turn_attempts" in tables
        assert "runtime_outbox" in tables
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version >= 3
        conn.close()

    def test_guidance_inbox_has_delivery_columns(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        migrate_connection(conn)
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "guidance_inbox" in tables
        columns = [
            row["name"] for row in conn.execute("PRAGMA table_info(guidance_inbox)")
        ]
        assert columns == [
            "guidance_id",
            "text",
            "source",
            "created_at",
            "target_session_id",
            "target_thread_id",
            "target_run_id",
            "target_phase",
            "delivery_id",
            "delivered_phase",
            "consumed_at",
            "truncated",
        ]
        assert conn.execute("PRAGMA user_version").fetchone()[0] >= 10
        conn.close()

    def test_goal_runtime_failure_and_public_summary_outbox_tables_exist(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        migrate_connection(conn)

        failure_columns = [
            row["name"]
            for row in conn.execute("PRAGMA table_info(goal_runtime_failures)")
        ]
        summary_columns = [
            row["name"]
            for row in conn.execute("PRAGMA table_info(goal_public_summary_outbox)")
        ]

        assert failure_columns == [
            "generation",
            "observed_sequence",
            "reason",
            "evidence_json",
            "created_at",
        ]
        assert summary_columns == [
            "summary_id",
            "generation",
            "main_session_id",
            "kind",
            "summary",
            "payload_json",
            "created_at",
            "delivered_at",
        ]
        assert conn.execute("PRAGMA user_version").fetchone()[0] >= 13
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
        migrate_connection(conn)
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


def test_v12_to_v13_adds_failure_contract_with_intended_foreign_keys() -> None:
    from voidx.persistence.migrations import MigrationPlan, MigrationRunner
    from voidx.persistence.sqlite import (
        MIGRATIONS,
        bootstrap_schema,
        canonicalize_core_schema,
        cleanup_legacy_payload_schema,
    )

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    MigrationRunner().migrate(
        conn,
        MigrationPlan(
            target_version=12,
            bootstrap_schema=(bootstrap_schema,),
            steps=MIGRATIONS[:12],
            cleanup=(cleanup_legacy_payload_schema, canonicalize_core_schema),
        ),
    )
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 12
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'goal_runtime_failures'"
    ).fetchone() is None
    conn.execute(
        """INSERT INTO sessions (id, created_at, updated_at)
           VALUES ('preserved-main', '2026-01-01', '2026-01-01')"""
    )
    conn.commit()

    migrate_connection(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == 14
    assert conn.execute(
        "SELECT id FROM sessions WHERE id = 'preserved-main'"
    ).fetchone() is not None
    indexes = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )
    }
    assert "idx_goal_public_summary_pending" in indexes
    failure_fks = [
        dict(row) for row in conn.execute("PRAGMA foreign_key_list(goal_runtime_failures)")
    ]
    assert failure_fks == [
        {
            "id": 0,
            "seq": 0,
            "table": "goal_generations",
            "from": "generation",
            "to": "generation",
            "on_update": "NO ACTION",
            "on_delete": "RESTRICT",
            "match": "NONE",
        }
    ]
    summary_fks = [
        dict(row)
        for row in conn.execute("PRAGMA foreign_key_list(goal_public_summary_outbox)")
    ]
    assert summary_fks == [
        {
            "id": 0,
            "seq": 0,
            "table": "sessions",
            "from": "main_session_id",
            "to": "id",
            "on_update": "NO ACTION",
            "on_delete": "CASCADE",
            "match": "NONE",
        }
    ]
    assert all(row["from"] != "generation" for row in summary_fks)
    summary_columns = {
        row["name"]: dict(row)
        for row in conn.execute("PRAGMA table_info(goal_public_summary_outbox)")
    }
    assert summary_columns["payload_json"]["notnull"] == 1
    assert summary_columns["payload_json"]["dflt_value"] == "'{}'"
    conn.close()
