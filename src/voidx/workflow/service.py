"""Workflow node selection and runtime context support."""

from __future__ import annotations

from pydantic import BaseModel

from voidx.workflow.context import (
    is_workflow_context_content,
    render_workflow_context,
    render_workflow_instruction,
    workflow_context_cache_key,
    workflow_body_hash,
)
from voidx.workflow.dag import DEFAULT_WORKFLOW_DAG
from voidx.workflow.policy import (
    is_workflow_terminal_condition,
    workflow_edges,
    workflow_exit_summaries,
    workflow_gate,
    workflow_sort_key,
    workflow_terminal_condition,
    workflow_terminal_description,
    workflow_transitions,
)
from voidx.workflow.schema import WorkflowNode
from voidx.workflow.types import WorkflowRunState, WorkflowRunStatus, source_from_reason


def advance_workflow_states(*args, **kwargs):
    from voidx.workflow.runtime import advance_workflow_states as _advance_workflow_states

    return _advance_workflow_states(*args, **kwargs)


def auto_advance_events(*args, **kwargs):
    from voidx.workflow.auto_advance import auto_advance_events as _auto_advance_events

    return _auto_advance_events(*args, **kwargs)


def reconcile_workflow_runs_for_turn(*args, **kwargs):
    from voidx.workflow.reconcile import (
        reconcile_workflow_runs_for_turn as _reconcile_workflow_runs_for_turn,
    )

    return _reconcile_workflow_runs_for_turn(*args, **kwargs)


def workflow_run_from_match(
    match: "WorkflowMatch",
    *,
    goal_type: str = "",
    scope: str = "",
    turn_count: int = 0,
    status: WorkflowRunStatus = WorkflowRunStatus.ACTIVE,
    workflow_body: str | None = None,
    body_hash: str = "",
) -> WorkflowRunState:
    body = match.body if workflow_body is None else workflow_body
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
        transition_to=list(workflow_transitions(match.name)),
    )


class WorkflowMatch(BaseModel):
    node: WorkflowNode
    reason: str

    @property
    def name(self) -> str:
        return self.node.name

    @property
    def body(self) -> str:
        return render_workflow_instruction(self.node)


class WorkflowService:
    def __init__(self) -> None:
        self._dag = DEFAULT_WORKFLOW_DAG

    def nodes(self) -> list[WorkflowNode]:
        return sorted(self._dag.nodes.values(), key=lambda node: workflow_sort_key(node.name))

    def get(self, name: str) -> WorkflowNode | None:
        return self._dag.nodes.get(_normalize(name))

    def select_from_start(
        self,
        workflow_start: str,
        *,
        goal_type: str | None = None,
    ) -> list[WorkflowMatch]:
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
                goal_type=goal_type or _goal_type_from_reason(match.reason),
                scope=scope,
            )
            for match in matches
        ]

    def context(self, *, active_names: Iterable[str] = ()) -> str:
        return render_workflow_context(self.nodes(), active_names=active_names)

    @staticmethod
    def render_instruction(node: WorkflowNode) -> str:
        return render_workflow_instruction(node)


def _normalize(name: str) -> str:
    return name.strip().lower()


def _goal_type_from_reason(reason: str) -> str:
    if reason.startswith("goal:"):
        return reason.removeprefix("goal:")
    return ""
