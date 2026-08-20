"""Workflow node selection and runtime context support."""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel

from voidx.agent.application.automation.workflow.context import (
    is_workflow_context_content,
    render_workflow_context,
    render_workflow_instruction,
    workflow_context_cache_key,
    workflow_body_hash,
)
from voidx.agent.domain.automation.workflow_policy import (
    is_workflow_terminal_condition,
    workflow_edges,
    workflow_exit_summaries,
    workflow_gate,
    workflow_personas,
    workflow_sort_key,
    workflow_terminal_condition,
    workflow_terminal_description,
    workflow_transitions,
)
from voidx.agent.domain.agent_profile import content_hash_of
from voidx.agent.domain.automation.workflow_schema import WorkflowDAG, WorkflowNode
from voidx.agent.domain.automation.workflow import WorkflowRunState, WorkflowRunStatus, source_from_reason


def advance_workflow_states(*args, **kwargs):
    from voidx.agent.application.automation.workflow.runtime import advance_workflow_states as _advance_workflow_states

    return _advance_workflow_states(*args, **kwargs)


def auto_advance_events(*args, **kwargs):
    from voidx.agent.application.automation.workflow.auto_advance import auto_advance_events as _auto_advance_events

    return _auto_advance_events(*args, **kwargs)


def reconcile_workflow_runs_for_turn(*args, **kwargs):
    from voidx.agent.application.automation.workflow.reconcile import (
        reconcile_workflow_runs_for_turn as _reconcile_workflow_runs_for_turn,
    )

    return _reconcile_workflow_runs_for_turn(*args, **kwargs)


def workflow_run_from_match(
    match: "WorkflowMatch",
    dag: WorkflowDAG,
    *,
    goal_type: str = "",
    scope: str = "",
    turn_count: int = 0,
    status: WorkflowRunStatus = WorkflowRunStatus.ACTIVE,
    workflow_body: str | None = None,
    body_hash: str = "",
) -> WorkflowRunState:
    body = render_workflow_instruction(match.node, dag) if workflow_body is None else workflow_body
    return WorkflowRunState(
        name=match.name,
        status=status,
        source=source_from_reason(match.reason),
        reason=match.reason,
        goal_type=goal_type,
        scope=scope,
        personas=[match.node.persona],
        activated_turn=turn_count,
        updated_turn=turn_count,
        body_hash=body_hash or (workflow_body_hash(body) if body else ""),
        dag_hash=content_hash_of(dag.model_dump(mode="json")),
        transition_to=list(workflow_transitions(match.name, dag)),
    )


class WorkflowMatch(BaseModel):
    node: WorkflowNode
    reason: str

    @property
    def name(self) -> str:
        return self.node.name


class WorkflowService:
    def __init__(self, dag: WorkflowDAG) -> None:
        self._dag = dag

    @property
    def dag(self) -> WorkflowDAG:
        return self._dag

    def nodes(self) -> list[WorkflowNode]:
        return sorted(self._dag.nodes.values(), key=lambda node: workflow_sort_key(node.name, self._dag))

    def get(self, name: str) -> WorkflowNode | None:
        return self._dag.nodes.get(_normalize(name))

    def select_from_start(
        self,
        workflow_start: str,
        *,
        goal_type: str | None = None,
    ) -> list[WorkflowMatch]:
        del goal_type
        name = _normalize(workflow_start)
        node = self.get(name)
        if node is None:
            return []
        return [WorkflowMatch(node=node, reason="goal_resolver")]

    def runs_from_matches(
        self,
        matches: list[WorkflowMatch],
        *,
        goal_type: str | None = None,
        scope: str = "",
    ) -> list[WorkflowRunState]:
        return [
            workflow_run_from_match(
                match,
                self._dag,
                goal_type=goal_type or _goal_type_from_reason(match.reason),
                scope=scope,
            )
            for match in matches
        ]

    def context(self, *, active_names: Iterable[str] = ()) -> str:
        return render_workflow_context(self.nodes(), self._dag, active_names=active_names)

    def render_instruction(self, node: WorkflowNode) -> str:
        return render_workflow_instruction(node, self._dag)


def _normalize(name: str) -> str:
    return name.strip().lower()


def _goal_type_from_reason(reason: str) -> str:
    if reason.startswith("goal:"):
        return reason.removeprefix("goal:")
    return ""


__all__ = [
    "WorkflowMatch",
    "WorkflowService",
    "advance_workflow_states",
    "auto_advance_events",
    "is_workflow_context_content",
    "is_workflow_terminal_condition",
    "reconcile_workflow_runs_for_turn",
    "render_workflow_context",
    "workflow_body_hash",
    "workflow_context_cache_key",
    "workflow_edges",
    "workflow_exit_summaries",
    "workflow_gate",
    "workflow_personas",
    "workflow_run_from_match",
    "workflow_sort_key",
    "workflow_terminal_condition",
    "workflow_terminal_description",
    "workflow_transitions",
]
