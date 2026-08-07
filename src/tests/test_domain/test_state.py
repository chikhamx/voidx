from voidx.agent.domain.state import SessionRuntimeState
from voidx.agent.domain.turn.state import TurnPhase, advance_turn
from voidx.agent.domain.task.state import GoalSpec, TaskState
from voidx.agent.domain.task.intent import InteractionMode, TaskIntent


def test_agent_runtime_owns_domain_state_without_graph() -> None:
    runtime = SessionRuntimeState(
        interaction_mode=InteractionMode.GOAL,
        task_state=TaskState(
            current_intent=TaskIntent.CODING,
            current_goal=GoalSpec(desc="unify agent state"),
        ),
        compaction_summary="existing summary",
        session_time="2026-07-19 CST",
    )

    assert runtime.task_state.current_goal == GoalSpec(desc="unify agent state")
    assert runtime.compaction_summary == "existing summary"
    assert runtime.session_time == "2026-07-19 CST"


def test_turn_state_conversion_is_pure_and_preserves_runtime() -> None:
    runtime = SessionRuntimeState(compaction_summary="summary")

    running = advance_turn(runtime, TurnPhase.RUNNING)
    committed = advance_turn(running, TurnPhase.COMMITTED)

    assert runtime.turn_phase is TurnPhase.INITIAL
    assert running.turn_phase is TurnPhase.RUNNING
    assert committed.turn_phase is TurnPhase.COMMITTED
    assert committed.compaction_summary == "summary"


def test_turn_state_rejects_invalid_transition() -> None:
    runtime = SessionRuntimeState(turn_phase=TurnPhase.COMMITTED)

    try:
        advance_turn(runtime, TurnPhase.RUNNING)
    except ValueError as exc:
        assert "committed -> running" in str(exc)
    else:
        raise AssertionError("invalid turn transition was accepted")
