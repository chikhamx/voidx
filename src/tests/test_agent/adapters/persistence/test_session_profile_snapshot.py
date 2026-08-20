"""Session persistence of resolved profile snapshots (Phase 2)."""

import sqlite3
from pathlib import Path

import pytest

from voidx.agent.adapters.persistence.provisional_sessions import (
    stage_provisional_session,
)
from voidx.agent.adapters.persistence.session_repository import (
    create_session,
    ensure_session,
    get_session,
    update_session_profile,
    validate_runtime_profile,
)
from voidx.agent.domain.agent_profile import AgentProfileSnapshot
from voidx.bootstrap.persistence import migrate_connection


def _snapshot(profile_id: str = "my-reviewer", revision: int = 3) -> AgentProfileSnapshot:
    return AgentProfileSnapshot(
        profile_id=profile_id,
        revision=revision,
        source="project",
        content_hash="a" * 64,
        snapshot_hash="b" * 64,
        canonical_payload={"name": profile_id, "revision": revision, "run_mode": "single"},
    )


def test_migrate_adds_profile_snapshot_columns(tmp_path: Path) -> None:
    conn = sqlite3.connect(str(tmp_path / "test.db"))
    migrate_connection(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    assert {
        "runtime_profile_revision",
        "runtime_profile_content_hash",
        "runtime_profile_hash",
        "runtime_profile_source",
        "runtime_profile_snapshot",
    } <= columns
    # Idempotent: a second migration run must not fail.
    migrate_connection(conn)
    conn.close()


async def test_create_session_persists_and_hydrates_snapshot() -> None:
    snapshot = _snapshot()
    info = await create_session(
        workspace=".", profile="my-reviewer", profile_snapshot=snapshot
    )

    loaded = await get_session(info.id)

    assert loaded is not None
    assert loaded.runtime_profile == "my-reviewer"
    assert loaded.profile_snapshot is not None
    assert loaded.profile_snapshot == snapshot


async def test_create_session_without_snapshot_pins_bundled_profile() -> None:
    info = await create_session(workspace=".", profile="coding")

    loaded = await get_session(info.id)

    assert loaded is not None
    assert loaded.profile_snapshot is not None
    assert loaded.profile_snapshot.profile_id == "coding"
    assert loaded.profile_snapshot.source == "bundled"


async def test_update_session_profile_stores_new_snapshot() -> None:
    info = await create_session(workspace=".", profile="coding")
    replacement = _snapshot(profile_id="my-reviewer", revision=4)

    await update_session_profile(info.id, "my-reviewer", profile_snapshot=replacement)

    loaded = await get_session(info.id)
    assert loaded is not None
    assert loaded.runtime_profile == "my-reviewer"
    assert loaded.profile_snapshot == replacement


async def test_ensure_session_persists_snapshot() -> None:
    snapshot = _snapshot()
    await ensure_session("sid-1", ".", profile="my-reviewer", profile_snapshot=snapshot)

    loaded = await get_session("sid-1")

    assert loaded is not None
    assert loaded.profile_snapshot == snapshot


async def test_provisional_session_persists_snapshot() -> None:
    snapshot = _snapshot()
    info = await stage_provisional_session(
        workspace=".",
        owner_id="goal:abc",
        profile="my-reviewer",
        profile_snapshot=snapshot,
    )

    loaded = await get_session(info.id)

    assert loaded is not None
    assert loaded.profile_snapshot == snapshot


def test_validate_runtime_profile_accepts_custom_names() -> None:
    assert validate_runtime_profile("coding") == "coding"
    assert validate_runtime_profile("my-reviewer") == "my-reviewer"


def test_validate_runtime_profile_rejects_malformed_names() -> None:
    with pytest.raises(ValueError, match="unknown runtime profile"):
        validate_runtime_profile("Invalid_Name!")
    with pytest.raises(ValueError, match="unknown runtime profile"):
        validate_runtime_profile("")
