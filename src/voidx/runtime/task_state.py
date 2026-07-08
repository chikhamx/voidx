"""Multi-turn task state shared across runtime layers."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from voidx.runtime.intent import InteractionMode, TaskIntent
from voidx.workflow.types import WorkflowRunState


_INTENT_WINDOW_SIZE = 4
_INTENT_WINDOW_SEPARATOR = " [SEP] "


class IntentResolution(BaseModel):
    model_config = {"extra": "ignore"}
    type: TaskIntent = TaskIntent.CODING


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
    intent: IntentResolution = Field(default_factory=lambda: IntentResolution(type=TaskIntent.CODING))
    goal: GoalSpec | None = None
    plan: PlanResolution | None = None


class WorkflowRoute(BaseModel):
    join: str = ""
    leave: str | None = None


class TodoRunItem(BaseModel):
    id: str = Field(..., max_length=20, description="Semantic id for the todo item")
    content: str
    status: Literal["pending", "active", "done"]


class TodoRunState(BaseModel):
    summary: str = ""
    total: int = 0
    done: int = 0
    active: int = 0
    pending: int = 0
    active_items: list[TodoRunItem] = Field(default_factory=list)
    items: list[TodoRunItem] = Field(default_factory=list)
    updated_at: str = ""


class TurnExchange(BaseModel):
    """Compact user/assistant pair retained for turn-level intent resolution."""

    user_text: str
    assistant_text: str = ""


class TaskState(BaseModel):
    current_intent: TaskIntent = TaskIntent.CODING
    previous_intent: TaskIntent | None = None
    current_goal: GoalSpec | None = None
    workflow_route: WorkflowRoute | None = None
    workflow_runs: dict[str, WorkflowRunState] = Field(default_factory=dict)
    recent_exchanges: list[TurnExchange] = Field(default_factory=list)
    todo_state: TodoRunState | None = None

    def update_after_turn(
        self,
        resolution: GoalResolution,
        user_text: str,
        *,
        scope_text: str | None = None,
    ) -> None:
        del scope_text
        del user_text
        previous_goal = self.current_goal
        self.previous_intent = self.current_intent
        self.current_intent = resolution.intent.type
        if resolution.intent.type == TaskIntent.GENERAL:
            if not self._has_active_workflow():
                if resolution.goal is not None:
                    self.current_goal = resolution.goal
                self._reset_workflow_context()
            elif resolution.goal is not None:
                self.current_goal = resolution.goal
            return
        if resolution.goal is not None:
            goal_changed = not _same_goal(previous_goal, resolution.goal)
            self.current_goal = resolution.goal
            if goal_changed:
                self._reset_workflow_context()
        self.workflow_route = _workflow_route_from_resolution(resolution)

    def set_goal(self, goal: GoalSpec | str | None) -> None:
        if goal is None:
            self.current_goal = None
            self._reset_workflow_context()
            return
        if isinstance(goal, GoalSpec):
            self.current_goal = goal
        else:
            self.current_goal = GoalSpec(desc=goal)
        self.current_intent = TaskIntent.CODING
        self._reset_workflow_context()

    def _has_active_workflow(self) -> bool:
        return any(
            getattr(run.status, "value", run.status) == "active"
            for run in self.workflow_runs.values()
        )

    def _reset_workflow_context(self) -> None:
        self.workflow_route = None
        self.workflow_runs = {}

    def clear_goal(self) -> None:
        self.set_goal(None)

    def merge_workflow_runs(self, runs: list[WorkflowRunState | dict]) -> None:
        for item in runs:
            run = item if isinstance(item, WorkflowRunState) else WorkflowRunState.model_validate(item)
            self.workflow_runs[run.name] = run

    def intent_window_text(self, current_text: str) -> str:
        current = _summarize_scope(current_text)
        previous = [
            _summarize_scope(exchange.user_text)
            for exchange in self.recent_exchanges[-(_INTENT_WINDOW_SIZE - 1):]
            if exchange.user_text
        ]
        previous = [
            item
            for item in previous
            if item
        ]
        parts = [*previous, current] if current else previous
        return _INTENT_WINDOW_SEPARATOR.join(parts[-_INTENT_WINDOW_SIZE:])


class ToolStatePatch(BaseModel):
    """Structured state updates requested by runtime tools."""

    intent: IntentResolution | None = None
    goal: GoalSpec | None = None
    plan: PlanResolution | None = None
    persona: str | None = None
    workflow_runs: list[WorkflowRunState] = Field(default_factory=list)


# ── workflow route helpers ──────────────────────────────────────────


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


# ── internal helpers ────────────────────────────────────────────────


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


def _summarize_scope(text: str) -> str:
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    return first_line[:160]


__all__ = [
    "InteractionMode",
    "TaskIntent",
    "GoalSpec",
    "IntentResolution",
    "PlanResolution",
    "GoalResolution",
    "WorkflowRoute",
    "TaskState",
    "TurnExchange",
    "TodoRunItem",
    "TodoRunState",
    "ToolStatePatch",
    "goal_label",
    "goal_type_from_join",
]
