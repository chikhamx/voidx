from __future__ import annotations

import hashlib
import json
import time
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage

import voidx.persistence.sqlite as store
from voidx.agent.adapters.langgraph.runtime.thread_context import (
    ThreadExecutionState,
    _CURRENT_THREAD_EXECUTION_STATE,
)
from voidx.agent.adapters.langgraph.runtime.turn_runner import _persist_new_messages
from voidx.agent.adapters.persistence.session_repository import (
    GoalTranscriptCorruption,
    MessageRow,
    append_goal_transcript_message,
    count_messages,
    create_session,
    load_goal_transcript_messages,
    save_message,
)
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.persistence.jsonl import session_dir


LEASE_OWNER = "goal-transcript-test"


async def _bind_goal_sessions() -> None:
    timestamp = store.now()
    await store.execute_commit(
        """INSERT INTO goal_generations (
               generation, main_session_id, evaluator_session_id, work_session_id,
               goal_thread_id, visibility, created_at
           ) VALUES (?, ?, ?, ?, ?, 'internal', ?)""",
        (
            "generation-1",
            "main-session",
            "evaluator-session",
            "work-session",
            "goal-thread-1",
            timestamp,
        ),
    )
    await store.execute_commit(
        """INSERT INTO agent_threads (
               id, parent_thread_id, session_id, workspace, profile_id,
               profile_revision, profile_json, resource_scope_json,
               created_at, updated_at
           ) VALUES (?, NULL, ?, '', 'goal', 1, '{}', '{}', ?, ?)""",
        ("goal-thread-1", "work-session", timestamp, timestamp),
    )


async def _activate_goal_attempt(
    attempt_id: str,
    *,
    session_id: str,
    attempt_number: int,
    fencing_token: int,
    lease_owner: str = LEASE_OWNER,
) -> None:
    phase = "work" if session_id == "work-session" else "evaluator"
    outbox_id = f"outbox-{attempt_id}"
    timestamp = store.now()
    claimed_until = time.time() + 60

    def _insert(conn) -> None:
        conn.execute(
            """INSERT INTO runtime_outbox (
                   id, thread_id, kind, payload_json, expected_state_version,
                   available_at, claimed_by, claimed_until, created_at
               ) VALUES (?, 'goal-thread-1', 'goal_prompt', '{}', 0, 0, ?, ?, ?)""",
            (outbox_id, lease_owner, claimed_until, timestamp),
        )
        conn.execute(
            """INSERT INTO runtime_turn_attempts (
                   id, thread_id, source_outbox_id, input_frame_json,
                   base_state_version, profile_id, profile_revision, status,
                   side_effect_started, lease_owner, fencing_token,
                   lease_expires_at, updated_at
               ) VALUES (?, 'goal-thread-1', ?, ?, 0, 'goal', 1, 'prepared',
                         1, ?, ?, ?, ?)""",
            (
                attempt_id,
                outbox_id,
                json.dumps(
                    {
                        "generation": "generation-1",
                        "phase": phase,
                        "attempt_number": attempt_number,
                    }
                ),
                lease_owner,
                fencing_token,
                claimed_until,
                timestamp,
            ),
        )

    await store.write_transaction(_insert)


@pytest.fixture
async def goal_sessions():
    for session_id in ("main-session", "evaluator-session", "work-session"):
        await create_session(session_id=session_id, profile="goal")
    await _bind_goal_sessions()


@pytest.mark.asyncio
async def test_goal_transcript_schema_has_accepted_index_constraints() -> None:
    await store.fetch_one("SELECT 1")

    columns = {
        row[1]
        for row in store._conn.execute("PRAGMA table_info(goal_transcript_records)").fetchall()
    }
    indexes = {
        row[1]: bool(row[2])
        for row in store._conn.execute("PRAGMA index_list(goal_transcript_records)").fetchall()
    }

    assert columns == {
        "session_id",
        "generation",
        "attempt_id",
        "attempt_number",
        "local_sequence",
        "session_sequence",
        "fencing_token",
        "filename",
        "start_offset",
        "end_offset",
        "payload_hash",
        "accepted_at",
    }
    assert indexes["idx_goal_transcript_order"] is False
    assert any(unique for unique in indexes.values())


@pytest.mark.asyncio
async def test_writer_uses_utf8_lf_byte_offsets_and_hydrates_only_accepted_rows(
    goal_sessions,
) -> None:
    await _activate_goal_attempt(
        "attempt-z",
        session_id="work-session",
        attempt_number=1,
        fencing_token=7,
    )
    first = await append_goal_transcript_message(
        session_id="work-session",
        generation="generation-1",
        attempt_id="attempt-z",
        attempt_number=1,
        local_sequence=1,
        lease_owner=LEASE_OWNER,
        fencing_token=7,
        message={"role": "assistant", "content": "你好"},
    )
    transcript_path = session_dir("work-session") / "messages.jsonl"
    orphan = b'{"role":"assistant","content":"orphan"}\n'
    with transcript_path.open("ab") as handle:
        handle.write(orphan)

    await _activate_goal_attempt(
        "attempt-a",
        session_id="work-session",
        attempt_number=2,
        fencing_token=8,
    )
    second = await append_goal_transcript_message(
        session_id="work-session",
        generation="generation-1",
        attempt_id="attempt-a",
        attempt_number=2,
        local_sequence=1,
        lease_owner=LEASE_OWNER,
        fencing_token=8,
        message={"role": "assistant", "content": "done"},
    )

    raw = transcript_path.read_bytes()
    first_payload = raw[first.start_offset:first.end_offset]
    second_payload = raw[second.start_offset:second.end_offset]
    assert first_payload.endswith(b"\n") and not first_payload.endswith(b"\n\n")
    assert second_payload.endswith(b"\n") and not second_payload.endswith(b"\n\n")
    assert len(first_payload) > len(first_payload.decode("utf-8"))
    assert second.start_offset == first.end_offset + len(orphan)
    assert first.payload_hash == hashlib.sha256(first_payload[:-1]).hexdigest()
    assert second.payload_hash == hashlib.sha256(second_payload[:-1]).hexdigest()

    hydrated = await load_goal_transcript_messages("work-session")
    assert [message.content for message in hydrated] == ["你好", "done"]
    assert [first.session_sequence, second.session_sequence] == [1, 2]
    assert await count_messages("work-session") == 2


@pytest.mark.asyncio
async def test_goal_transcript_retry_is_idempotent_and_conflicting_payload_fails(
    goal_sessions,
) -> None:
    await _activate_goal_attempt(
        "attempt-1",
        session_id="evaluator-session",
        attempt_number=1,
        fencing_token=4,
    )
    kwargs = {
        "session_id": "evaluator-session",
        "generation": "generation-1",
        "attempt_id": "attempt-1",
        "attempt_number": 1,
        "local_sequence": 3,
        "lease_owner": LEASE_OWNER,
        "fencing_token": 4,
    }
    first = await append_goal_transcript_message(
        **kwargs,
        message={"role": "assistant", "content": "accepted"},
    )
    retried = await append_goal_transcript_message(
        **kwargs,
        message={"role": "assistant", "content": "accepted"},
    )

    assert retried == first
    assert await count_messages("evaluator-session") == 1
    assert len((session_dir("evaluator-session") / "messages.jsonl").read_bytes().splitlines()) == 1

    with pytest.raises(ValueError, match="conflict"):
        await append_goal_transcript_message(
            **kwargs,
            message={"role": "assistant", "content": "different"},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["hash", "truncated", "offset"])
async def test_goal_transcript_corruption_fails_closed(goal_sessions, corruption: str) -> None:
    await _activate_goal_attempt(
        "attempt-1",
        session_id="work-session",
        attempt_number=1,
        fencing_token=2,
    )
    accepted = await append_goal_transcript_message(
        session_id="work-session",
        generation="generation-1",
        attempt_id="attempt-1",
        attempt_number=1,
        local_sequence=1,
        lease_owner=LEASE_OWNER,
        fencing_token=2,
        message={"role": "assistant", "content": "accepted"},
    )
    transcript_path = session_dir("work-session") / "messages.jsonl"

    if corruption == "hash":
        await store.execute_commit(
            """UPDATE goal_transcript_records SET payload_hash = ?
               WHERE session_id = ? AND session_sequence = ?""",
            ("0" * 64, "work-session", accepted.session_sequence),
        )
    elif corruption == "truncated":
        transcript_path.write_bytes(transcript_path.read_bytes()[:-2])
    else:
        await store.execute_commit(
            """UPDATE goal_transcript_records SET end_offset = end_offset + 100
               WHERE session_id = ? AND session_sequence = ?""",
            ("work-session", accepted.session_sequence),
        )

    with pytest.raises(GoalTranscriptCorruption, match="canonical transcript"):
        await load_goal_transcript_messages("work-session")


@pytest.mark.asyncio
async def test_goal_transcript_rejects_non_child_binding(goal_sessions) -> None:
    await _activate_goal_attempt(
        "attempt-1",
        session_id="work-session",
        attempt_number=1,
        fencing_token=1,
    )
    with pytest.raises(ValueError, match="binding"):
        await append_goal_transcript_message(
            session_id="main-session",
            generation="generation-1",
            attempt_id="attempt-1",
            attempt_number=1,
            local_sequence=1,
            lease_owner=LEASE_OWNER,
            fencing_token=1,
            message={"role": "assistant", "content": "not internal"},
        )

    assert not session_dir("main-session").exists()


@pytest.mark.asyncio
async def test_goal_transcript_rejects_stale_attempt_fencing(goal_sessions) -> None:
    await _activate_goal_attempt(
        "attempt-current",
        session_id="work-session",
        attempt_number=1,
        fencing_token=9,
    )

    with pytest.raises(ValueError, match="lease conflict"):
        await append_goal_transcript_message(
            session_id="work-session",
            generation="generation-1",
            attempt_id="attempt-current",
            attempt_number=1,
            local_sequence=1,
            lease_owner=LEASE_OWNER,
            fencing_token=8,
            message={"role": "assistant", "content": "stale"},
        )

    assert await count_messages("work-session") == 0
    assert not session_dir("work-session").exists()


@pytest.mark.asyncio
async def test_runtime_message_persistence_uses_accepted_goal_transcript(goal_sessions) -> None:
    await _activate_goal_attempt(
        "attempt-runtime",
        session_id="work-session",
        attempt_number=1,
        fencing_token=5,
    )
    context = TurnExecutionContext(
        thread_id="goal-thread-1",
        session_id="work-session",
        goal_phase="work",
        goal_generation="generation-1",
        goal_attempt_id="attempt-runtime",
        goal_attempt_number=1,
        goal_lease_owner=LEASE_OWNER,
        goal_fencing_token=5,
    )
    state = ThreadExecutionState(thread_id="goal-thread-1", turn_context=context)
    token = _CURRENT_THREAD_EXECUTION_STATE.set(state)
    host = SimpleNamespace(
        _session=SimpleNamespace(id="work-session"),
        _session_msg_cache=[],
    )
    try:
        await _persist_new_messages(
            host,
            [
                AIMessage(content="accepted assistant"),
                ToolMessage(content="accepted tool", tool_call_id="call-1"),
            ],
        )
    finally:
        _CURRENT_THREAD_EXECUTION_STATE.reset(token)

    hydrated = await load_goal_transcript_messages("work-session")
    assert [(row.role, row.content) for row in hydrated] == [
        ("assistant", "accepted assistant"),
        ("tool", "accepted tool"),
    ]


@pytest.mark.asyncio
async def test_generic_save_message_rejects_goal_internal_session(goal_sessions) -> None:
    with pytest.raises(ValueError, match="accepted transcript"):
        await save_message(
            MessageRow(
                session_id="work-session",
                role="assistant",
                content="bypass",
            )
        )
