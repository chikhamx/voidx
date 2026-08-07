"""Shared fixtures and helpers for test_agent tests."""

import json
import sqlite3
import sys
from pathlib import Path


import pytest

import voidx.persistence.sqlite as store


@pytest.fixture(autouse=True)
def isolated_memory_store(tmp_path):
    if store._conn is not None:
        store._conn.close()
    store._conn = None
    store.DATA_DIR = tmp_path / ".voidx"
    yield
    if store._conn is not None:
        store._conn.close()
    store._conn = None


@pytest.fixture(autouse=True)
def simulated_llm_retry_sleep(monkeypatch: pytest.MonkeyPatch):
    def simulated_delay(_delay: float) -> float:
        return 0.002

    monkeypatch.setattr(
        "voidx.agent.infrastructure.langgraph.runtime.core.loop._llm_retry_sleep_delay",
        simulated_delay,
    )
    monkeypatch.setattr(
        "voidx.agent.infrastructure.langgraph.runtime.subagent._llm_retry_sleep_delay",
        simulated_delay,
    )


@pytest.fixture(autouse=True)
def call_llm_renderer_patch_compat(monkeypatch: pytest.MonkeyPatch):
    class RendererProxy:
        def __new__(cls, *args, **kwargs):
            from voidx.runtime.ui import StreamingRenderer

            return StreamingRenderer(*args, **kwargs)

    monkeypatch.setattr(
        "voidx.agent.infrastructure.langgraph.runtime.llm_turn.StreamingRenderer",
        RendererProxy,
    )


def _session_dir(session_id: str) -> Path:
    return store.DATA_DIR / "sessions" / session_id


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def _table_names() -> set[str]:
    rows = await store._fetch_all("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {str(row["name"]) for row in rows}


async def _table_columns(table: str) -> set[str]:
    rows = await store._fetch_all(f"PRAGMA table_info({table})")
    return {str(row["name"]) for row in rows}


def _create_legacy_db(path: Path, session_id: str, *, title: str = "Legacy") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            """CREATE TABLE sessions (
                   id TEXT PRIMARY KEY,
                   title TEXT NOT NULL DEFAULT 'New session',
                   workspace TEXT NOT NULL DEFAULT '.',
                   model_provider TEXT NOT NULL DEFAULT 'anthropic',
                   model_name TEXT NOT NULL DEFAULT 'claude-sonnet-4-6',
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL
               )"""
        )
        conn.execute(
            """CREATE TABLE messages (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   session_id TEXT NOT NULL,
                   role TEXT NOT NULL,
                   content TEXT NOT NULL DEFAULT '',
                   tool_calls TEXT,
                   tool_call_id TEXT,
                   created_at TEXT NOT NULL
               )"""
        )
        conn.execute(
            """INSERT INTO sessions (
                   id, title, workspace, model_provider, model_name, created_at, updated_at
               )
               VALUES (?, ?, '.', 'anthropic', 'claude-sonnet-4-6', '2026-06-14T00:00:00+00:00', '2026-06-14T00:00:00+00:00')""",
            (session_id, title),
        )
        conn.execute(
            """INSERT INTO messages (session_id, role, content, created_at)
               VALUES (?, 'user', 'hello', '2026-06-14T00:00:00+00:00')""",
            (session_id,),
        )
        conn.commit()
    finally:
        conn.close()


def _create_new_store_db(path: Path, session_id: str, *, title: str = "New Store") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        store._init_schema(conn)
        conn.execute(
            """INSERT INTO sessions (
                   id, title, workspace, model_provider, model_name, created_at, updated_at, message_count
               )
               VALUES (?, ?, '.', 'anthropic', 'claude-sonnet-4-6', '2026-06-14T00:00:00+00:00', '2026-06-14T00:00:00+00:00', 0)""",
            (session_id, title),
        )
        conn.commit()
    finally:
        conn.close()
