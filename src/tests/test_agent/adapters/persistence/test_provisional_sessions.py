from __future__ import annotations

import sqlite3

import pytest

from voidx.agent.adapters.persistence.provisional_sessions import (
    cleanup_orphaned_provisional_sessions,
    find_orphaned_provisional_roots,
    get_provisional_session,
    promote_provisional_session,
    rollback_provisional_session,
    stage_provisional_session,
)
from voidx.agent.adapters.persistence.session_repository import (
    MessageRow,
    create_session,
    get_session,
    latest_session_for_workspace,
    list_sessions,
    save_message,
)
from voidx.persistence.jsonl import session_dir


@pytest.mark.asyncio
async def test_schema_v4_creates_provisional_session_markers():
    import voidx.persistence.sqlite as store

    await store.fetch_one("SELECT 1")

    assert store.SCHEMA_VERSION == 14
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 14
    columns = {
        row[1]: row[2]
        for row in store._conn.execute("PRAGMA table_info(provisional_sessions)").fetchall()
    }
    assert columns == {
        "session_id": "TEXT",
        "root_session_id": "TEXT",
        "owner_id": "TEXT",
        "created_at": "TEXT",
    }


@pytest.mark.asyncio
async def test_stage_accepts_identity_profile_and_workspace_and_is_hidden():
    visible = await create_session(workspace="/work")
    staged = await stage_provisional_session(
        session_id="provisional-fixed",
        owner_id="worker-1",
        workspace="/work",
        profile="goal",
    )

    assert staged.id == "provisional-fixed"
    assert staged.workspace == "/work"
    assert staged.runtime_profile == "goal"
    assert await get_session(staged.id) == staged
    assert {session.id for session in await list_sessions()} == {visible.id}
    assert (await latest_session_for_workspace("/work")).id == visible.id




@pytest.mark.asyncio
async def test_stage_is_idempotent_for_same_owner_and_session():
    first = await stage_provisional_session(
        session_id="idempotent-stage",
        owner_id="worker-1",
        workspace="/work",
        profile="goal",
    )

    second = await stage_provisional_session(
        session_id="idempotent-stage",
        owner_id="worker-1",
        workspace="/work",
        profile="goal",
    )

    assert second == first
    marker = await get_provisional_session(first.id)
    assert marker is not None
    assert marker.owner_id == "worker-1"
@pytest.mark.asyncio
async def test_derived_marker_inherits_root_and_promote_root_reveals_group():
    root = await stage_provisional_session(
        session_id="provisional-root",
        owner_id="worker-1",
        workspace="/work",
    )
    child = await stage_provisional_session(
        session_id="provisional-child",
        root_session_id=root.id,
        owner_id="worker-2",
        workspace="/work",
    )
    grandchild = await stage_provisional_session(
        session_id="provisional-grandchild",
        root_session_id=child.id,
        owner_id="worker-3",
        workspace="/work",
    )

    assert (await get_provisional_session(root.id)).root_session_id == root.id
    assert (await get_provisional_session(child.id)).root_session_id == root.id
    assert (await get_provisional_session(grandchild.id)).root_session_id == root.id

    assert await promote_provisional_session(root.id) == 3
    assert await get_provisional_session(child.id) is None
    assert {session.id for session in await list_sessions()} == {
        root.id,
        child.id,
        grandchild.id,
    }




@pytest.mark.asyncio
async def test_ensure_derived_session_inherits_provisional_root():
    from voidx.agent.adapters.persistence.session_repository import ensure_session

    root = await stage_provisional_session(
        session_id="derived-root",
        owner_id="worker-1",
        profile="goal",
    )

    await ensure_session(
        "goal-derived-root-generation",
        "/work",
        profile="goal",
        root_session_id=root.id,
    )

    marker = await get_provisional_session("goal-derived-root-generation")
    assert marker is not None
    assert marker.root_session_id == root.id
    assert marker.owner_id == "worker-1"
    assert {session.id for session in await list_sessions()} == set()
@pytest.mark.asyncio
async def test_rollback_root_deletes_group_data_and_jsonl_directories():
    root = await stage_provisional_session(
        session_id="rollback-root",
        owner_id="worker-1",
    )
    child = await stage_provisional_session(
        session_id="rollback-child",
        root_session_id=root.id,
        owner_id="worker-1",
    )
    await save_message(MessageRow(session_id=root.id, role="user", content="root"))
    await save_message(MessageRow(session_id=child.id, role="user", content="child"))

    import voidx.persistence.sqlite as store

    timestamp = "2026-01-01T00:00:00+00:00"
    await store.execute_commit(
        """INSERT INTO agent_threads
           (id, session_id, workspace, profile_id, profile_revision, profile_json,
            resource_scope_json, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "rollback-thread",
            child.id,
            ".",
            "coding",
            1,
            "{}",
            "{}",
            timestamp,
            timestamp,
        ),
    )
    assert session_dir(root.id).exists()
    assert session_dir(child.id).exists()

    assert await rollback_provisional_session(root.id) == 2

    assert await get_session(root.id) is None
    assert await get_session(child.id) is None
    assert await store.fetch_one(
        "SELECT 1 FROM agent_threads WHERE id = ?", ("rollback-thread",)
    ) is None
    assert not session_dir(root.id).exists()
    assert not session_dir(child.id).exists()


@pytest.mark.asyncio
async def test_promote_or_rollback_child_operates_on_entire_root_group():
    root = await stage_provisional_session(session_id="group-root", owner_id="worker")
    child = await stage_provisional_session(
        session_id="group-child",
        root_session_id=root.id,
        owner_id="worker",
    )

    assert await rollback_provisional_session(child.id) == 2
    assert await get_session(root.id) is None
    assert await get_session(child.id) is None


@pytest.mark.asyncio
async def test_orphan_discovery_requires_cutoff_and_active_owner_set():
    root = await stage_provisional_session(session_id="old-root", owner_id="dead")
    await stage_provisional_session(
        session_id="old-child",
        root_session_id=root.id,
        owner_id="active",
    )
    await stage_provisional_session(session_id="active-root", owner_id="active")

    import voidx.persistence.sqlite as store

    await store.execute_commit(
        "UPDATE provisional_sessions SET created_at = ? WHERE root_session_id = ?",
        ("2026-01-01T00:00:00+00:00", root.id),
    )
    await store.execute_commit(
        "UPDATE provisional_sessions SET created_at = ? WHERE session_id = ?",
        ("2026-01-01T00:00:00+00:00", "active-root"),
    )

    assert await find_orphaned_provisional_roots(
        active_owner_ids={"active"},
        created_before="2026-02-01T00:00:00+00:00",
    ) == []
    assert await find_orphaned_provisional_roots(
        active_owner_ids=set(),
        created_before="2026-02-01T00:00:00+00:00",
    ) == ["active-root", "old-root"]


@pytest.mark.asyncio
async def test_orphan_cleanup_is_dry_run_by_default_and_requires_explicit_apply():
    root = await stage_provisional_session(session_id="cleanup-root", owner_id="dead")

    import voidx.persistence.sqlite as store

    await store.execute_commit(
        "UPDATE provisional_sessions SET created_at = ? WHERE session_id = ?",
        ("2026-01-01T00:00:00+00:00", root.id),
    )

    candidates = await cleanup_orphaned_provisional_sessions(
        active_owner_ids=set(),
        created_before="2026-02-01T00:00:00+00:00",
    )
    assert candidates == [root.id]
    assert await get_session(root.id) is not None

    deleted = await cleanup_orphaned_provisional_sessions(
        active_owner_ids=set(),
        created_before="2026-02-01T00:00:00+00:00",
        dry_run=False,
    )
    assert deleted == [root.id]
    assert await get_session(root.id) is None


@pytest.mark.asyncio
async def test_owner_lifecycle_preserves_live_owner_and_cleans_dead_owner():
    from voidx.agent.adapters.persistence import provisional_sessions as provisional

    await stage_provisional_session(session_id="live-root", owner_id="live-owner")
    await stage_provisional_session(session_id="dead-root", owner_id="dead-owner")
    provisional.register_provisional_owner("live-owner")
    provisional.register_provisional_owner("dead-owner", pid=999_999_999)

    assert provisional.active_provisional_owner_ids() == {"live-owner"}

    cleaned = await provisional.cleanup_dead_provisional_owners()
    assert cleaned == ["dead-root"]
    assert await get_session("live-root") is not None
    assert await get_session("dead-root") is None

    assert await provisional.close_provisional_owner("live-owner") == 1
    assert await get_session("live-root") is None
    assert provisional.active_provisional_owner_ids() == set()


def test_migration_from_v3_adds_marker_table():
    import voidx.persistence.sqlite as store

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    store.bootstrap_schema(conn)
    conn.execute("DROP TABLE provisional_sessions")
    conn.execute("PRAGMA user_version=3")

    store._init_schema(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == 14
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'provisional_sessions'"
    ).fetchone() is not None


@pytest.mark.asyncio
async def test_close_owner_preserves_shared_root_while_another_owner_marker_remains():
    from voidx.agent.adapters.persistence import provisional_sessions as provisional

    root = await stage_provisional_session(session_id="shared-root", owner_id="owner-a")
    await stage_provisional_session(
        session_id="shared-child",
        root_session_id=root.id,
        owner_id="owner-b",
    )
    provisional.register_provisional_owner("owner-a")
    provisional.register_provisional_owner("owner-b")

    assert await provisional.close_provisional_owner("owner-a") == 0

    assert await get_session("shared-root") is not None
    assert await get_session("shared-child") is not None
    root_marker = await get_provisional_session("shared-root")
    child_marker = await get_provisional_session("shared-child")
    assert root_marker is not None
    assert child_marker is not None
    assert root_marker.owner_id == "owner-b"
    assert child_marker.owner_id == "owner-b"

    assert await provisional.close_provisional_owner("owner-b") == 2
    assert await get_session("shared-root") is None
    assert await get_session("shared-child") is None


def test_owner_marker_with_reused_live_pid_but_no_held_lease_is_stale():
    import json
    import os

    from voidx.agent.adapters.persistence import provisional_sessions as provisional

    directory = provisional._owner_directory()
    directory.mkdir(parents=True, exist_ok=True)
    path = provisional._owner_path("reused-pid")
    path.write_text(
        json.dumps({"owner_id": "reused-pid", "pid": os.getpid()}),
        encoding="utf-8",
    )

    assert provisional.active_provisional_owner_ids() == set()
    assert not path.exists()


def test_owner_marker_write_failure_releases_new_lease(monkeypatch):
    from pathlib import Path

    from voidx.agent.adapters.persistence import provisional_sessions as provisional

    original_write_text = Path.write_text

    def fail_owner_marker(path, *args, **kwargs):
        if path.name == "write-failure.tmp":
            raise OSError("disk full")
        return original_write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_owner_marker)

    with pytest.raises(OSError, match="disk full"):
        provisional.register_provisional_owner("write-failure")

    assert "write-failure" not in provisional._OWNER_LEASES
    assert not provisional._owner_path("write-failure").exists()
    assert not provisional._owner_path("write-failure").with_suffix(".tmp").exists()
