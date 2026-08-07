from __future__ import annotations

import sqlite3

import pytest

from voidx.agent.adapters.persistence.thread_repository import ThreadStore
from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.domain.thread import AgentThread, RuntimeDecision


@pytest.mark.asyncio
async def test_commit_decision_rolls_back_state_attempt_and_outbox_on_mid_transaction_failure(
    tmp_path,
) -> None:
    store = ThreadStore(db_path=tmp_path / "thread.db")
    await store.create_thread(
        AgentThread(thread_id="loop-rollback"),
        profile=RuntimeProfile(profile_id="loop", revision=1, name="Loop"),
    )
    loaded = await store.load("loop-rollback")
    assert loaded is not None
    attempt = await store.begin_attempt(
        thread_id="loop-rollback",
        source_outbox_id="source-rollback",
        input_frame={"prompt": "fixed"},
        expected_state_version=loaded.state_version,
        lease_owner="worker-a",
        lease_seconds=60,
    )

    before_commit = await store.load("loop-rollback")
    assert before_commit is not None
    assert before_commit.state_version == attempt.state_version

    connection = store._conn
    connection.execute(
        """CREATE TEMP TRIGGER fail_wakeup_insert
           BEFORE INSERT ON runtime_outbox
           WHEN NEW.kind = 'wakeup'
           BEGIN
               SELECT RAISE(ABORT, 'injected wakeup failure');
           END"""
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected wakeup failure"):
        await store.commit_decision(
            attempt_id=attempt.attempt_id,
            decision=RuntimeDecision(outcome="continue", summary="continue"),
            expected_state_version=attempt.state_version,
            lease_owner="worker-a",
            fencing_token=attempt.fencing_token,
        )
    connection.execute("DROP TRIGGER fail_wakeup_insert")

    after = await store.load("loop-rollback")
    assert after is not None
    assert after.state_version == before_commit.state_version
    assert after.state == before_commit.state
    attempt_row = connection.execute(
        "SELECT status FROM runtime_turn_attempts WHERE id = ?", (attempt.attempt_id,)
    ).fetchone()
    assert attempt_row["status"] == "prepared"
    outbox_count = connection.execute(
        "SELECT COUNT(*) FROM runtime_outbox WHERE source_attempt_id = ? AND kind = 'wakeup'",
        (attempt.attempt_id,),
    ).fetchone()[0]
    assert outbox_count == 0
