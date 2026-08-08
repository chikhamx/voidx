from voidx.agent.domain.state import SessionRuntimeState
from voidx.agent.adapters.persistence.runtime_state_mapper import (
    agent_runtime_from_snapshot,
    snapshot_from_agent_runtime,
)
from voidx.agent.adapters.persistence.runtime_state_repository import RuntimeStateSnapshot
from voidx.agent.domain.task.state import GoalSpec, TaskState
from voidx.agent.domain.task.intent import InteractionMode, TaskIntent
from voidx.agent.domain.task.todo import TodoRunState
from voidx.agent.domain.automation.workflow import WorkflowRoute


def test_agent_runtime_snapshot_round_trip_preserves_persisted_json() -> None:
    snapshot = RuntimeStateSnapshot(
        interaction_mode=InteractionMode.GOAL,
        task_state=TaskState(
            current_intent=TaskIntent.CODING,
            previous_intent=TaskIntent.GENERAL,
            current_goal=GoalSpec(desc="unify agent state"),
            workflow_route=WorkflowRoute(join="tdd", leave="verify"),
            todo_state=TodoRunState.model_validate(
                {
                    "summary": "0/1 done · 1 active",
                    "total": 1,
                    "active": 1,
                    "active_items": [
                        {"id": "domain", "content": "create domain", "status": "active"}
                    ],
                    "items": [
                        {"id": "domain", "content": "create domain", "status": "active"}
                    ],
                    "updated_at": "2026-07-19T00:00:00+08:00",
                }
            ),
        ),
        compaction_summary="summary",
        session_time="2026-07-19 CST",
    )

    runtime = agent_runtime_from_snapshot(snapshot)
    restored = snapshot_from_agent_runtime(runtime)

    assert isinstance(runtime, SessionRuntimeState)
    assert restored.model_dump(mode="json") == snapshot.model_dump(mode="json")
