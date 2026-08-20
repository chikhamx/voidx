from __future__ import annotations

from langchain_core.messages import RemoveMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from voidx.agent.domain.task.intent import PersonaName
from voidx.agent.domain.task.state import ToolStatePatch
from voidx.agent.domain.automation.workflow import WorkflowRoute
from voidx.agent.domain.automation.workflow_schema import WorkflowDAG
from voidx.agent.application.automation.workflow.service import advance_workflow_states, auto_advance_events
from voidx.agent.application.automation.workflow.route import (
    workflow_path_reaches,
    workflow_route_end,
    workflow_route_start,
    workflow_transition_target,
)
from voidx.agent.domain.automation.workflow import (
    WorkflowEvidence,
    WorkflowRunState,
    WorkflowRunStatus,
    WorkflowStateEvent,
    WorkflowStateEventKind,
)

from .types import _ExecutedTool


def _state_update_from_executed_tools(
    executed: list[_ExecutedTool],
    *,
    current_workflow_runs: object = (),
    current_workflow_route: object = None,
    turn_count: int = 0,
    workflow_dag: WorkflowDAG | None = None,
) -> dict:
    update: dict = {}
    merged_workflow_runs = _merge_workflow_runs_for_state(current_workflow_runs)
    workflow_runs_changed = False
    for item in executed:
        if item.tool_call.get("name") == "todo" and item.todo_state is not None:
            if item.todo_state.total > 0:
                update["todo_state"] = item.todo_state.model_dump(mode="json")
            else:
                update["todo_state"] = None

        metadata = getattr(item.result, "metadata", {}) or {}
        raw = metadata.get("state_patch")
        if raw is None:
            continue
        patch = ToolStatePatch.model_validate(raw)
        data = patch.model_dump(mode="json")
        for field in patch.model_fields_set:
            if field == "intent":
                value = data.get(field)
                if value is not None:
                    update["task_intent"] = value.get("type") or "coding"
            elif field == "workflow_runs":
                route_limited = _explicit_advance_route_limited_runs(
                    item,
                    merged_workflow_runs,
                    current_workflow_route=current_workflow_route,
                    turn_count=turn_count,
                    workflow_dag=workflow_dag,
                )
                if route_limited is not None:
                    merged_workflow_runs = route_limited
                    update["should_continue"] = False
                else:
                    merged_workflow_runs = _merge_workflow_runs_for_state(
                        merged_workflow_runs,
                        patch.workflow_runs,
                    )
                workflow_runs_changed = True
            elif field == "goal":
                update["current_goal"] = data.get(field)
            elif field == "plan":
                value = data.get(field)
                update["workflow_route"] = value if value is not None else None
            elif field == "persona":
                update["persona"] = data.get(field) or PersonaName.COORDINATE

    # Auto-advance: detect structured tool result signals and drive DAG
    # transitions without explicit workflow.
    auto_events = (
        _auto_advance_from_executed(executed, merged_workflow_runs, workflow_dag)
        if workflow_dag is not None
        else []
    )
    if auto_events:
        merged_workflow_runs, stop_after_auto = _advance_auto_events_for_route(
            merged_workflow_runs,
            auto_events,
            current_workflow_route=current_workflow_route,
            turn_count=turn_count,
            workflow_dag=workflow_dag,
        )
        workflow_runs_changed = True
        if stop_after_auto:
            update["should_continue"] = False

    if workflow_runs_changed:
        update["workflow_runs"] = merged_workflow_runs
    return update


async def _inline_compaction_messages(host, messages: list, executed: list[_ExecutedTool]) -> list:
    summary = _inline_compaction_summary(executed)
    if not summary:
        return []

    async def use_submitted_summary(_head_messages, _previous_summary):
        return summary

    # The LLM has already produced the summary via compact, so this
    # path bypasses budget gating and only reuses the coordinator's split,
    # persistence, and live-message replacement logic.
    result = await host._compaction_component().compact_for_live_state(
        list(messages),
        force=True,
        ask=False,
        include_summary_message=True,
        run_compaction_agent=use_submitted_summary,
        persist_compaction=host._persist_compaction,
    )
    if result is None:
        return []
    return [
        RemoveMessage(id=REMOVE_ALL_MESSAGES),
        *result.live_messages,
    ]


def _inline_compaction_summary(executed: list[_ExecutedTool]) -> str:
    for item in executed:
        metadata = getattr(item.result, "metadata", {}) or {}
        raw = metadata.get("inline_compaction")
        if not isinstance(raw, dict):
            continue
        summary = str(raw.get("summary") or "").strip()
        if summary:
            return summary
    return ""


def _auto_advance_from_executed(
    executed: list[_ExecutedTool],
    workflow_runs: list[WorkflowRunState],
    workflow_dag: WorkflowDAG,
) -> list:
    """Check executed tools for auto-advance signals and return events."""
    tool_items = []
    for item in executed:
        tool_items.append({
            "name": item.tool_call.get("name", ""),
            "result": item.result,
        })
    return auto_advance_events(tool_items, workflow_runs=workflow_runs, dag=workflow_dag)


def _explicit_advance_route_limited_runs(
    item: _ExecutedTool,
    workflow_runs: list[WorkflowRunState],
    *,
    current_workflow_route: object = None,
    turn_count: int = 0,
    workflow_dag: WorkflowDAG | None = None,
) -> list[WorkflowRunState] | None:
    if workflow_dag is None or item.tool_call.get("name") != "workflow":
        return None
    metadata = getattr(item.result, "metadata", {}) or {}
    transition = metadata.get("workflow_transition") or {}
    if not isinstance(transition, dict):
        return None
    if str(transition.get("action") or "").strip().lower() != "advance":
        return None
    workflow = str(transition.get("from") or "").strip().lower()
    condition = str(transition.get("condition") or "").strip().lower()
    target = workflow_transition_target(workflow, condition, workflow_dag)
    if not _auto_event_satisfies_route_terminal(
        workflow,
        target,
        route_start=workflow_route_start(current_workflow_route),
        route_end=workflow_route_end(current_workflow_route),
        workflow_dag=workflow_dag,
    ):
        return None
    event = WorkflowStateEvent(
        workflow=workflow,
        kind=WorkflowStateEventKind.SATISFIED,
        ref="tool:workflow",
        ok=True,
        summary=str(transition.get("summary") or ""),
        reason=str(transition.get("evidence") or ""),
        condition=condition,
    )
    return _satisfy_workflow_without_transition(workflow_runs, event, turn_count=turn_count)


def _advance_auto_events_for_route(
    workflow_runs: list[WorkflowRunState],
    auto_events: list,
    *,
    current_workflow_route: object = None,
    turn_count: int = 0,
    workflow_dag: WorkflowDAG,
) -> tuple[list[WorkflowRunState], bool]:
    route_start = workflow_route_start(current_workflow_route)
    route_end = workflow_route_end(current_workflow_route)
    runs = list(workflow_runs)
    should_stop = False
    for event in auto_events:
        target = workflow_transition_target(event.workflow, event.condition, workflow_dag)
        if _auto_event_satisfies_route_terminal(
            event.workflow,
            target,
            route_start=route_start,
            route_end=route_end,
            workflow_dag=workflow_dag,
        ):
            runs = _satisfy_workflow_without_transition(runs, event, turn_count=turn_count)
            should_stop = True
            continue
        runs = advance_workflow_states(runs, [event], dag=workflow_dag)
        if _auto_event_should_stop_after_transition(
            event.ok,
            target,
            route_end=route_end,
            workflow_dag=workflow_dag,
        ):
            should_stop = True
    return runs, should_stop


def _auto_event_satisfies_route_terminal(
    workflow: str,
    target: str,
    *,
    route_start: str,
    route_end: str,
    workflow_dag: WorkflowDAG,
) -> bool:
    if not route_end or workflow != route_end:
        return False
    if not target:
        return True
    if route_start and route_start != route_end and workflow_path_reaches(target, route_end, workflow_dag):
        return False
    return True


def _auto_event_should_stop_after_transition(
    ok: bool | None,
    target: str,
    *,
    route_end: str,
    workflow_dag: WorkflowDAG,
) -> bool:
    if route_end:
        return bool(target) and target != route_end and not workflow_path_reaches(target, route_end, workflow_dag)
    return ok is False


def _satisfy_workflow_without_transition(
    workflow_runs: list[WorkflowRunState],
    event,
    *,
    turn_count: int = 0,
) -> list[WorkflowRunState]:
    target = str(getattr(event, "workflow", "") or "").strip().lower()
    updated: list[WorkflowRunState] = []
    for run in workflow_runs:
        copy = run.model_copy(deep=True)
        if copy.name.strip().lower() != target or copy.status != WorkflowRunStatus.ACTIVE:
            updated.append(copy)
            continue
        evidence = WorkflowEvidence(
            kind=event.kind.value,
            ref=event.ref,
            ok=event.ok,
            summary=event.summary,
            condition=event.condition,
        )
        if evidence not in copy.evidence:
            copy.evidence.append(evidence)
        copy.status = WorkflowRunStatus.SATISFIED
        copy.updated_turn = turn_count
        copy.blocked_reason = ""
        updated.append(copy)
    return updated


def _merge_workflow_runs_for_state(*groups: object) -> list[WorkflowRunState]:
    merged: dict[str, WorkflowRunState] = {}
    for group in groups:
        items = group.values() if isinstance(group, dict) else group or []
        for item in items:
            try:
                run = item if isinstance(item, WorkflowRunState) else WorkflowRunState.model_validate(item)
            except (TypeError, ValueError):
                continue
            key = run.name.strip().lower()
            if not key:
                continue
            normalized = run.model_copy(deep=True)
            normalized.name = key
            merged[key] = normalized
    return list(merged.values())
