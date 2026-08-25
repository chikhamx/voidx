from __future__ import annotations

import pytest

from voidx.agent.adapters.persistence.thread_repository import ThreadStore
from voidx.agent.domain.guidance import Guidance
from voidx.agent.ports.persistence import GuidanceConflict


@pytest.fixture
def store(tmp_path) -> ThreadStore:
    return ThreadStore(tmp_path / "guidance.db")


@pytest.mark.asyncio
async def test_guidance_submit_is_durable_and_idempotent(store: ThreadStore) -> None:
    guidance = Guidance(
        guidance_id="guidance-1",
        text="use the smaller API",
        target_session_id="session-1",
        source="user",
    )

    saved = await store.submit_guidance(guidance)
    repeated = await store.submit_guidance(guidance)

    assert saved == guidance
    assert repeated == guidance
    assert await store.get_guidance("guidance-1") == guidance

    with pytest.raises(GuidanceConflict):
        await store.submit_guidance(guidance.model_copy(update={"text": "replace me"}))


@pytest.mark.asyncio
async def test_guidance_binding_matches_targets_and_is_claimed_once(store: ThreadStore) -> None:
    await store.submit_guidance(
        Guidance(
            guidance_id="session-guidance",
            text="session note",
            target_session_id="session-1",
        )
    )
    await store.submit_guidance(
        Guidance(
            guidance_id="work-guidance",
            text="work note",
            target_run_id="run-1",
            target_phase="work",
        )
    )
    await store.submit_guidance(
        Guidance(
            guidance_id="evaluator-guidance",
            text="evaluator note",
            target_run_id="run-1",
            target_phase="evaluator",
        )
    )

    work = await store.bind_guidance(
        "attempt-1",
        session_id="session-1",
        run_id="run-1",
        phase="work",
    )
    assert [item.guidance_id for item in work] == [
        "session-guidance",
        "work-guidance",
    ]
    assert all(item.delivery_id == "attempt-1" for item in work)

    second_worker = await store.bind_guidance(
        "attempt-2",
        session_id="session-1",
        run_id="run-1",
        phase="work",
    )
    assert second_worker == []

    evaluator = await store.bind_guidance(
        "attempt-3",
        session_id="session-1",
        run_id="run-1",
        phase="evaluator",
    )
    assert [item.guidance_id for item in evaluator] == ["evaluator-guidance"]


@pytest.mark.asyncio
async def test_guidance_release_allows_redelivery_and_consume_is_terminal(
    store: ThreadStore,
) -> None:
    guidance = Guidance(
        guidance_id="guidance-1",
        text="preserve this note",
        target_thread_id="thread-1",
    )
    await store.submit_guidance(guidance)

    bound = await store.bind_guidance("delivery-1", thread_id="thread-1")
    assert [item.guidance_id for item in bound] == ["guidance-1"]

    await store.release_guidance("delivery-1")
    rebound = await store.bind_guidance("delivery-2", thread_id="thread-1")
    assert [item.guidance_id for item in rebound] == ["guidance-1"]

    await store.consume_guidance("delivery-2")
    consumed = await store.get_guidance("guidance-1")
    assert consumed is not None
    assert consumed.delivery_id == "delivery-2"
    assert consumed.consumed_at is not None
    assert await store.bind_guidance("delivery-3", thread_id="thread-1") == []


@pytest.mark.asyncio
async def test_guidance_persists_truncated_flag_and_v10_schema(store: ThreadStore) -> None:
    assert store._conn is not None
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] >= 10
    columns = {
        row[1]: row[2]
        for row in store._conn.execute("PRAGMA table_info(guidance_inbox)").fetchall()
    }
    assert columns["truncated"] == "INTEGER"

    guidance = Guidance(
        guidance_id="truncated-guidance",
        text="shortened note",
        truncated=True,
        target_thread_id="thread-1",
    )
    await store.submit_guidance(guidance)

    assert await store.get_guidance(guidance.guidance_id) == guidance
    assert await store.submit_guidance(guidance) == guidance
