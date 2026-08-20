from voidx.agent.domain.automation.workflow_dag import DEFAULT_WORKFLOW_DAG
from voidx.agent.adapters.langgraph.runtime.tool_executor.workflow import (
    _merge_workflow_runs_for_state,
    _satisfy_workflow_without_transition,
    _state_update_from_executed_tools,
)
from voidx.agent.adapters.langgraph.runtime.tool_executor.types import _ExecutedTool
from voidx.agent.domain.task.state import ToolStatePatch
from voidx.tooling.domain.result import ToolResult
from voidx.agent.domain.automation.workflow import (
    WorkflowRunState,
    WorkflowRunStatus,
    WorkflowStateEvent,
    WorkflowStateEventKind,
)


def _executed_workflow_patch(patch: ToolStatePatch, *, action: str = "advance"):
    return _ExecutedTool(
        message=None,
        result=ToolResult(
            output="workflow result",
            metadata={"state_patch": patch.model_dump(mode="json", exclude_unset=True)},
        ),
        tool_call={"name": "workflow", "args": {"action": action}},
    )


def test_workflow_patch_restores_advance_state_across_runtime_rounds():
    current = [
        WorkflowRunState(
            name="tdd",
            status=WorkflowRunStatus.ACTIVE,
            transition_to=["verify"],
            goal="验证实现",
        )
    ]
    patch_runs = [
        current[0].model_copy(update={"status": WorkflowRunStatus.SATISFIED}),
        WorkflowRunState(name="verify", status=WorkflowRunStatus.ACTIVE, goal="验证实现"),
    ]

    update = _state_update_from_executed_tools(
        [_executed_workflow_patch(ToolStatePatch(workflow_runs=patch_runs))],
        current_workflow_runs=current,
    workflow_dag=DEFAULT_WORKFLOW_DAG)

    restored = {run.name: run for run in update["workflow_runs"]}
    assert restored["tdd"].status == WorkflowRunStatus.SATISFIED
    assert restored["verify"].status == WorkflowRunStatus.ACTIVE
    assert restored["verify"].goal == "验证实现"


def test_replaying_same_workflow_patch_is_idempotent():
    current = [WorkflowRunState(name="tdd", status=WorkflowRunStatus.ACTIVE)]
    satisfied = current[0].model_copy(
        update={
            "status": WorkflowRunStatus.SATISFIED,
            "evidence": [{
                "kind": "satisfied",
                "ref": "tool:workflow",
                "ok": True,
                "summary": "Workflow node tdd completed.",
                "condition": "implemented",
            }],
        }
    )
    patch = ToolStatePatch(workflow_runs=[satisfied])
    executed = [_executed_workflow_patch(patch)]

    first = _state_update_from_executed_tools(executed, current_workflow_runs=current, workflow_dag=DEFAULT_WORKFLOW_DAG)
    second = _state_update_from_executed_tools(
        executed,
        current_workflow_runs=first["workflow_runs"],
    workflow_dag=DEFAULT_WORKFLOW_DAG)

    first_run = first["workflow_runs"][0]
    second_run = second["workflow_runs"][0]
    assert first_run.status == WorkflowRunStatus.SATISFIED
    assert second_run.status == WorkflowRunStatus.SATISFIED
    assert len(second_run.evidence) == len(first_run.evidence) == 1


def test_done_patch_restores_all_closed_workflows_without_successors():
    current = [
        WorkflowRunState(name="debug", status=WorkflowRunStatus.ACTIVE),
        WorkflowRunState(name="tdd", status=WorkflowRunStatus.ACTIVE),
    ]
    patch_runs = [
        run.model_copy(update={"status": WorkflowRunStatus.SATISFIED})
        for run in current
    ]

    update = _state_update_from_executed_tools(
        [_executed_workflow_patch(ToolStatePatch(workflow_runs=patch_runs), action="done")],
        current_workflow_runs=current,
    workflow_dag=DEFAULT_WORKFLOW_DAG)

    assert {run.name: run.status for run in update["workflow_runs"]} == {
        "debug": WorkflowRunStatus.SATISFIED,
        "tdd": WorkflowRunStatus.SATISFIED,
    }


def test_route_terminal_satisfaction_is_idempotent():
    runs = [WorkflowRunState(name="review", status=WorkflowRunStatus.ACTIVE)]
    event = WorkflowStateEvent(
        workflow="review",
        kind=WorkflowStateEventKind.SATISFIED,
        ref="tool:workflow",
        ok=True,
        summary="route completed",
        condition="terminal",
    )

    first = _satisfy_workflow_without_transition(runs, event, turn_count=4)
    second = _satisfy_workflow_without_transition(first, event, turn_count=5)

    assert second[0].status == WorkflowRunStatus.SATISFIED
    assert len(second[0].evidence) == 1
    assert second[0].updated_turn == 4


def test_route_terminal_satisfaction_matches_normalized_workflow_name():
    runs = [WorkflowRunState(name="Review", status=WorkflowRunStatus.ACTIVE)]
    event = WorkflowStateEvent(
        workflow=" review ",
        kind=WorkflowStateEventKind.SATISFIED,
        ref="tool:workflow",
        ok=True,
        summary="route completed",
        condition="terminal",
    )

    updated = _satisfy_workflow_without_transition(runs, event, turn_count=4)

    assert updated[0].status == WorkflowRunStatus.SATISFIED
    assert len(updated[0].evidence) == 1


def test_merge_workflow_runs_normalizes_names_and_keeps_latest_state():
    merged = _merge_workflow_runs_for_state(
        [WorkflowRunState(name="Review", status=WorkflowRunStatus.ACTIVE)],
        [WorkflowRunState(name=" review ", status=WorkflowRunStatus.SATISFIED)],
        [WorkflowRunState(name="review", status=WorkflowRunStatus.BLOCKED)],
    )

    assert len(merged) == 1
    assert merged[0].name == "review"
    assert merged[0].status == WorkflowRunStatus.BLOCKED


def test_merge_workflow_runs_keeps_latest_snapshot_fields_intact():
    previous = WorkflowRunState(
        name="tdd",
        status=WorkflowRunStatus.ACTIVE,
        goal="旧目标",
        transition_to=["verify"],
        evidence=[
            {
                "kind": "activated",
                "ref": "old",
                "ok": True,
                "summary": "old evidence",
                "condition": "enter",
            }
        ],
    )
    latest = WorkflowRunState(
        name=" TDD ",
        status=WorkflowRunStatus.SATISFIED,
        goal="新目标",
        transition_to=["review"],
        evidence=[
            {
                "kind": "satisfied",
                "ref": "new",
                "ok": True,
                "summary": "new evidence",
                "condition": "implemented",
            }
        ],
    )

    merged = _merge_workflow_runs_for_state([previous], [latest])

    assert len(merged) == 1
    assert merged[0].status == WorkflowRunStatus.SATISFIED
    assert merged[0].goal == "新目标"
    assert merged[0].transition_to == ["review"]
    assert [item.ref for item in merged[0].evidence] == ["new"]
