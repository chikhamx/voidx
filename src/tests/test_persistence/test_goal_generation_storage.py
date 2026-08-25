from __future__ import annotations

import pytest

from voidx.agent.adapters.persistence.session_repository import (
    create_session,
    delete_session,
    list_sessions,
)
from voidx.agent.adapters.persistence.thread_repository import ThreadStore
from voidx.agent.domain.agent_profile import AgentProfileSnapshot
from voidx.agent.domain.automation.goal import (
    GOAL_PROFILE,
    GoalProtocolRecord,
    GoalSpec,
    GoalSpecSnapshot,
    GoalState,
)
from voidx.agent.domain.thread import AgentThreadState, LifecycleState


@pytest.fixture
def store() -> ThreadStore:
    return ThreadStore()


def _profile_snapshot() -> AgentProfileSnapshot:
    return AgentProfileSnapshot(
        profile_id="goal",
        revision=1,
        source="bundled",
        content_hash="content",
        snapshot_hash="snapshot",
        canonical_payload={"profile_id": "goal"},
    )


def _boundary_kwargs() -> dict:
    spec = GoalSpec(
        objective="ship feature",
        acceptance_condition="tests pass",
        generation="gen-storage",
    )
    snapshot = GoalSpecSnapshot.from_spec(
        spec,
        parent_session_id="main-storage",
        profile_snapshot={"profile_id": "goal"},
    )
    state = GoalState.from_spec(
        spec,
        run_id=spec.generation,
        main_session_id="main-storage",
        work_session_id="work-storage",
        evaluator_session_id="eval-storage",
    )
    return {
        "generation": spec.generation,
        "main_session_id": "main-storage",
        "evaluator_session_id": "eval-storage",
        "work_session_id": "work-storage",
        "goal_thread_id": "goal:main-storage:gen-storage",
        "parent_thread_id": "main-thread",
        "workspace": "/workspace",
        "profile_id": "goal",
        "profile_snapshot": _profile_snapshot(),
        "thread_profile": GOAL_PROFILE,
        "thread_state": AgentThreadState(
            thread_id="goal:main-storage:gen-storage",
            lifecycle=LifecycleState.READY,
            context={
                "goal_spec": spec.model_dump(mode="json"),
                "goal_run": state.model_dump(mode="json"),
            },
        ),
        "protocol": GoalProtocolRecord.submitted(
            protocol_id="protocol-init-storage",
            parent_session_id="main-storage",
            generation=spec.generation,
            phase="init",
            attempt_number=0,
            turn_id="turn-init-storage",
            session_id="main-storage",
            payload=snapshot,
        ),
    }


@pytest.mark.asyncio
async def test_generation_binding_can_be_loaded_after_restart(store: ThreadStore) -> None:
    await store.ensure_session(
        "main-storage", "/workspace", profile="goal", profile_snapshot=_profile_snapshot()
    )
    await store.initialize_goal_generation(**_boundary_kwargs())

    binding = await store.get_goal_generation("gen-storage")

    assert binding is not None
    assert binding.main_session_id == "main-storage"
    assert binding.work_session_id == "work-storage"
    assert binding.evaluator_session_id == "eval-storage"
    assert binding.visibility == "internal"


@pytest.mark.asyncio
async def test_regular_session_listing_hides_goal_child_sessions_but_keeps_main(
    store: ThreadStore,
) -> None:
    await store.ensure_session(
        "main-storage", "/workspace", profile="goal", profile_snapshot=_profile_snapshot()
    )
    await store.initialize_goal_generation(**_boundary_kwargs())

    sessions = await list_sessions()
    ids = {session.id for session in sessions}

    assert "main-storage" in ids
    assert "work-storage" not in ids
    assert "eval-storage" not in ids


@pytest.mark.asyncio
async def test_direct_delete_of_goal_child_session_is_rejected(store: ThreadStore) -> None:
    await store.ensure_session(
        "main-storage", "/workspace", profile="goal", profile_snapshot=_profile_snapshot()
    )
    await store.initialize_goal_generation(**_boundary_kwargs())

    with pytest.raises(ValueError, match="internal"):
        await delete_session("work-storage")
