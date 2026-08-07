from __future__ import annotations

import json
from pathlib import Path

import pytest

import voidx.persistence.sqlite as store
from voidx.agent.domain.automation.goal import GoalSpec, GoalState
from voidx.agent.domain.automation.loop import LoopSpec
from voidx.agent.domain.automation.workflow import WorkflowRunState
from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.domain.state import SessionRuntimeState
from voidx.agent.domain.task.todo import TodoRunState
from voidx.agent.domain.thread import AgentThreadState, RuntimeOutboxItem, ThreadAttempt
from voidx.persistence.jsonl import write_session_json, write_session_records


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "persistence"


def payload_manifest() -> dict[str, object]:
    goal_spec = GoalSpec(
        objective="Fixed objective",
        acceptance_condition="Fixed acceptance",
        generation="fixed",
    )
    return {
        "runtime": SessionRuntimeState(session_time="2026-08-05 UTC").model_dump(mode="json"),
        "workflow_runs": [WorkflowRunState(name="debug").model_dump(mode="json")],
        "todo": TodoRunState().model_dump(mode="json"),
        "goal": GoalState.from_spec(goal_spec, run_id="goal-fixed").model_dump(mode="json"),
        "loop": LoopSpec(prompt="Fixed prompt", generation="fixed").model_dump(mode="json"),
        "compaction": {"summary": "Fixed summary"},
        "session_time": {"session_time": "2026-08-05 UTC"},
        "profile": RuntimeProfile(profile_id="coding", revision=1, name="Coding").model_dump(mode="json"),
        "thread": AgentThreadState(thread_id="thread-fixed").model_dump(mode="json"),
        "attempt": ThreadAttempt(
            attempt_id="attempt-fixed",
            thread_id="thread-fixed",
            source_outbox_id="outbox-source-fixed",
            state_version=1,
            fencing_token=2,
            lease_owner="worker-fixed",
            status="prepared",
        ).model_dump(mode="json"),
        "outbox": RuntimeOutboxItem(
            outbox_id="outbox-fixed",
            thread_id="thread-fixed",
            kind="wakeup",
            payload={"prompt": "Fixed prompt"},
            expected_state_version=1,
        ).model_dump(mode="json"),
    }


def test_json_payload_manifest_exact() -> None:
    expected = json.loads((FIXTURES / "payloads.json").read_text(encoding="utf-8"))
    assert payload_manifest() == expected


@pytest.mark.asyncio
async def test_jsonl_payloads_are_byte_exact(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    session_id = "session-fixed"
    records = {
        "messages.jsonl": [{"id": "message-fixed", "role": "user", "content": "hello"}],
        "runtime.jsonl": [{"session_time": "2026-08-05 UTC", "turn": 1}],
        "runtime_debug.jsonl": [{"event": "turn_started", "turn": 1}],
        "context/deletes.jsonl": [{"frame_id": "frame-fixed"}],
        "context/frame-fixed.jsonl": [{"role": "system", "content": "fixed"}],
        "subagents/records.jsonl": [{"run_id": "run-fixed", "status": "completed"}],
        "transcript.jsonl": [{"node_id": "node-fixed", "text": "fixed"}],
    }
    for filename, rows in records.items():
        await write_session_records(session_id, filename, rows)
    await write_session_json(
        session_id,
        "transcript.idx.json",
        {"node-fixed": 0},
    )
    await write_session_json(
        session_id,
        "transcript.checkpoint.json",
        {"offset": 0, "node_id": "node-fixed"},
    )

    actual_root = tmp_path / "sessions" / session_id
    expected_root = FIXTURES / "jsonl"
    expected_files = sorted(path.relative_to(expected_root) for path in expected_root.rglob("*") if path.is_file())
    assert expected_files == sorted([*(Path(name) for name in records), Path("transcript.idx.json"), Path("transcript.checkpoint.json")])
    for relative in expected_files:
        assert (actual_root / relative).read_bytes() == (expected_root / relative).read_bytes()
