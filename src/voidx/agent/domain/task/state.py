"""Goal resolution and mutable Agent task state."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, model_validator

from voidx.agent.domain.automation.workflow import WorkflowRunState, WorkflowRoute
from voidx.agent.domain.task.intent import InteractionMode
from voidx.agent.domain.task.todo import TodoRunItem, TodoRunState


class WorkflowContextMode(str, Enum):
    ACTIVE = "active"
    NONE = "none"


class GoalSpec(BaseModel):
    model_config = {"extra": "ignore"}
    desc: str = ""

    @model_validator(mode="after")
    def _normalize_desc(self) -> "GoalSpec":
        self.desc = " ".join(self.desc.split())[:120]
        return self

    @property
    def label(self) -> str:
        return self.desc.strip() or ""


class PlanResolution(BaseModel):
    join: str
    leave: str | None = None


class GoalResolution(BaseModel):
    goal: GoalSpec | None = None
    plan: PlanResolution | None = None


class TurnExchange(BaseModel):
    """Compact user/assistant pair retained for turn-level goal resolution."""

    user_text: str
    assistant_text: str = ""


class TaskState(BaseModel):
    current_goal: GoalSpec | None = None
    workflow_route: WorkflowRoute | None = None
    workflow_runs: dict[str, WorkflowRunState] = Field(default_factory=dict)
    workflow_context_mode: WorkflowContextMode = WorkflowContextMode.NONE
    recent_exchanges: list[TurnExchange] = Field(default_factory=list)
    todo_state: TodoRunState | None = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_workflow_context_mode(cls, value):
        if isinstance(value, dict) and value.get("workflow_context_mode") == "paused":
            value = dict(value)
            value["workflow_context_mode"] = WorkflowContextMode.NONE
        return value

    @model_validator(mode="after")
    def _default_workflow_context_mode(self) -> "TaskState":
        if "workflow_context_mode" not in self.model_fields_set:
            self.workflow_context_mode = (
                WorkflowContextMode.ACTIVE
                if self._has_active_workflow() or self.workflow_route is not None
                else WorkflowContextMode.NONE
            )
            self.model_fields_set.discard("workflow_context_mode")
        return self

    def update_after_turn(self, resolution: GoalResolution) -> None:
        previous_goal = self.current_goal
        resolved_goal = resolution.goal
        if resolved_goal is not None:
            goal_changed = not _same_goal(previous_goal, resolved_goal)
            self.current_goal = resolved_goal
            if goal_changed:
                self._reset_workflow_context()

        route = _workflow_route_from_resolution(resolution)
        if route is not None and route.join:
            self.workflow_route = route
            self.workflow_context_mode = WorkflowContextMode.ACTIVE
            return

        if self.workflow_context_mode != WorkflowContextMode.ACTIVE:
            self.workflow_context_mode = WorkflowContextMode.NONE
            self.workflow_route = None
        else:
            self.workflow_route = None

    def set_goal(self, goal: GoalSpec | str | None) -> None:
        if goal is None:
            self.current_goal = None
            self._reset_workflow_context()
            return
        if isinstance(goal, GoalSpec):
            self.current_goal = goal
        else:
            self.current_goal = GoalSpec(desc=goal)
        self._reset_workflow_context()

    def _has_active_workflow(self) -> bool:
        return any(
            getattr(run.status, "value", run.status) == "active"
            for run in self.workflow_runs.values()
        )

    def visible_workflow_runs(self) -> list[WorkflowRunState]:
        if self.workflow_context_mode != WorkflowContextMode.ACTIVE:
            return []
        return list(self.workflow_runs.values())

    def _reset_workflow_context(self) -> None:
        self.workflow_route = None
        self.workflow_runs = {}
        self.workflow_context_mode = WorkflowContextMode.NONE

    def clear_goal(self) -> None:
        self.set_goal(None)

    def merge_workflow_runs(self, runs: list[WorkflowRunState | dict]) -> None:
        for item in runs:
            run = item if isinstance(item, WorkflowRunState) else WorkflowRunState.model_validate(item)
            self.workflow_runs[run.name] = run


class ToolStatePatch(BaseModel):
    """Structured state updates requested by runtime tools."""

    goal: GoalSpec | None = None
    plan: PlanResolution | None = None
    persona: str | None = None
    workflow_runs: list[WorkflowRunState] = Field(default_factory=list)


def _workflow_route_from_resolution(resolution: GoalResolution) -> WorkflowRoute | None:
    plan = resolution.plan
    if plan is None:
        return None
    return WorkflowRoute(join=plan.join, leave=plan.leave)


def _same_goal(left: GoalSpec | None, right: GoalSpec | None) -> bool:
    if left is None or right is None:
        return left is right
    return left.desc == right.desc


def goal_label(goal: GoalSpec | dict | None) -> str:
    value = _coerce_goal(goal)
    return value.label if value is not None else ""


_JOIN_GOAL_TYPE_MAP: dict[str, str] = {
    "brainstorm": "design",
    "debug": "debug",
    "design": "doc",
    "feedback": "review",
    "plan": "design",
    "review": "review",
    "tdd": "feature",
    "verify": "feature",
}


def goal_type_from_join(join: str | None) -> str:
    if not join:
        return ""
    return _JOIN_GOAL_TYPE_MAP.get(join, "")


def _coerce_goal(goal: GoalSpec | dict | None) -> GoalSpec | None:
    if goal is None:
        return None
    if isinstance(goal, GoalSpec):
        return goal
    if isinstance(goal, dict):
        try:
            return GoalSpec.model_validate({k: v for k, v in goal.items() if k in GoalSpec.model_fields})
        except ValueError:
            return None
    return None



__all__ = [
    "InteractionMode",
    "GoalSpec",
    "PlanResolution",
    "GoalResolution",
    "WorkflowContextMode",
    "WorkflowRoute",
    "TaskState",
    "TurnExchange",
    "TodoRunItem",
    "TodoRunState",
    "ToolStatePatch",
    "goal_label",
    "goal_type_from_join",
]


# End of module.
