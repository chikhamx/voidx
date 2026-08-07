from voidx.agent.application.automation.workflow.runtime import advance_workflow_states
from voidx.agent.application.automation.workflow.service import workflow_terminal_condition, workflow_transitions
from voidx.agent.domain.automation.workflow import (
    WorkflowActivationSource,
    WorkflowRunState,
    WorkflowRunStatus,
    WorkflowStateEvent,
    WorkflowStateEventKind,
)


def _satisfied_event(workflow: str, condition: str) -> WorkflowStateEvent:
    return WorkflowStateEvent(
        workflow=workflow,
        kind=WorkflowStateEventKind.SATISFIED,
        ref="test:workflow",
        ok=True,
        summary=f"completed {workflow}",
        condition=condition,
    )


def test_repeated_satisfied_event_does_not_duplicate_evidence():
    runs = [WorkflowRunState(name="tdd", status=WorkflowRunStatus.ACTIVE)]
    event = _satisfied_event("tdd", "implemented")

    first = advance_workflow_states(runs, [event], turn_count=3)
    second = advance_workflow_states(first, [event], turn_count=4)

    tdd = second[0]
    assert tdd.status == WorkflowRunStatus.SATISFIED
    assert len(tdd.evidence) == 1
    assert tdd.updated_turn == 3




def test_existing_transition_metadata_is_preserved_and_successor_is_deduplicated():
    runs = [
        WorkflowRunState(
            name="tdd",
            status=WorkflowRunStatus.ACTIVE,
            transition_to=["stale", "verify", "verify"],
        )
    ]

    updated = advance_workflow_states(
        runs,
        [_satisfied_event("tdd", "implemented")],
    )

    by_name = {run.name: run for run in updated}
    assert by_name["tdd"].transition_to == ["stale", "verify", "verify"]
    assert by_name["verify"].status == WorkflowRunStatus.ACTIVE
    assert list(by_name).count("verify") == 1


def test_missing_transition_metadata_is_filled_from_current_dag():
    runs = [WorkflowRunState(name="tdd", status=WorkflowRunStatus.ACTIVE)]

    updated = advance_workflow_states(
        runs,
        [_satisfied_event("tdd", "implemented")],
    )

    tdd = {run.name: run for run in updated}["tdd"]
    assert tdd.transition_to == list(workflow_transitions("tdd"))


def test_transition_successor_inherits_goal_from_active_run():
    runs = [
        WorkflowRunState(
            name="tdd",
            status=WorkflowRunStatus.ACTIVE,
            goal="验证实现",
            source=WorkflowActivationSource.MANUAL,
        )
    ]

    updated = advance_workflow_states(
        runs,
        [_satisfied_event("tdd", "implemented")],
    )

    verify = {run.name: run for run in updated}["verify"]
    assert verify.status == WorkflowRunStatus.ACTIVE
    assert verify.goal == "验证实现"


def test_terminal_event_does_not_activate_transition_successors():
    runs = [
        WorkflowRunState(
            name="review",
            status=WorkflowRunStatus.ACTIVE,
            transition_to=["tdd"],
        )
    ]

    updated = advance_workflow_states(
        runs,
        [_satisfied_event("review", workflow_terminal_condition())],
    )

    assert {run.name for run in updated} == {"review"}
    assert updated[0].status == WorkflowRunStatus.SATISFIED
