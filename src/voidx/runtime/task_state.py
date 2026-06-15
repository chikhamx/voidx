"""Multi-turn task state shared across runtime layers."""

from __future__ import annotations

import re

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from voidx.runtime.intent import InteractionMode, TaskIntent, infer_task_intent
from voidx.workflow.types import WorkflowRunState


_INTENT_WINDOW_SIZE = 2
_INTENT_WINDOW_SEPARATOR = " [SEP] "


_WRITE_HINTS = (
    "apply",
    "change",
    "edit",
    "fix",
    "implement",
    "modify",
    "patch",
    "refactor",
    "write",
    "\u6539",
    "\u4fee",
    "\u4fee\u590d",
    "\u4fee\u6539",
    "\u5b9e\u73b0",
    "\u843d\u5730",
    "\u7ee7\u7eed\u6539",
    "\u7ee7\u7eed\u505a",
)

_REVIEW_HINTS = ("review", "code review", "\u5ba1\u67e5", "\u590d\u6838", "\u8bc4\u5ba1")
_DEBUG_HINTS = (
    "debug",
    "traceback",
    "stacktrace",
    "\u62a5\u9519",
    "\u6392\u67e5",
    "\u8c03\u8bd5",
    "\u5f02\u5e38",
)
_BUGFIX_HINTS = ("bug", "failing", "failure", "failed", "\u6545\u969c", "\u9519\u8bef", "\u95ee\u9898")
_REFACTOR_HINTS = ("refactor", "rename", "cleanup", "\u91cd\u6784", "\u6539\u540d", "\u6e05\u7406")
_FEATURE_HINTS = ("feature", "add", "support", "\u65b0\u589e", "\u6dfb\u52a0", "\u652f\u6301")
_DOC_HINTS = ("doc", "docs", "readme", "spec", "\u6587\u6863", "\u89c4\u683c", "\u8bf4\u660e")
_DESIGN_HINTS = (
    "design",
    "plan",
    "proposal",
    "approach",
    "architecture",
    "suggest",
    "\u8bbe\u8ba1",
    "\u65b9\u6848",
    "\u5efa\u8bae",
    "\u600e\u4e48\u6539",
    "\u5982\u4f55\u6539",
    "\u8ba8\u8bba",
    "\u89c4\u5212",
)
_INSPECT_HINTS = (
    "look at",
    "inspect",
    "analyze",
    "explain",
    "understand",
    "check",
    "\u770b\u770b",
    "\u770b\u4e00\u4e0b",
    "\u5206\u6790",
    "\u68b3\u7406",
    "\u4e86\u89e3",
    "\u68c0\u67e5",
    "\u73b0\u72b6",
)


class GoalType(str, Enum):
    BUGFIX = "bugfix"
    DEBUG = "debug"
    REFACTOR = "refactor"
    FEATURE = "feature"
    CHORE = "chore"
    INSPECT = "inspect"
    DESIGN = "design"
    DOC = "doc"
    REVIEW = "review"


class IntentResolution(BaseModel):
    type: TaskIntent = TaskIntent.CODING
    desc: str = ""


class GoalSpec(BaseModel):
    type: GoalType
    desc: str = ""

    @property
    def label(self) -> str:
        return self.desc.strip() or self.type.value


class PlanResolution(BaseModel):
    join: str
    leave: str | None = None


class GoalResolution(BaseModel):
    intent: IntentResolution = Field(default_factory=lambda: IntentResolution(type=TaskIntent.CODING, desc=""))
    goal: GoalSpec | None = None
    plan: PlanResolution | None = None


class WorkflowRoute(BaseModel):
    join: str = ""
    leave: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_names(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        if "join" in value or "leave" in value:
            return value
        migrated = dict(value)
        if "start" in migrated:
            migrated["join"] = migrated.get("start")
        if "end" in migrated:
            migrated["leave"] = migrated.get("end")
        return migrated


class TodoRunItem(BaseModel):
    content: str
    status: Literal["pending", "in_progress", "completed", "cancelled"]


class TodoRunState(BaseModel):
    summary: str = ""
    items: list[TodoRunItem] = Field(default_factory=list)
    updated_at: str = ""


class TaskState(BaseModel):
    current_intent: TaskIntent = TaskIntent.CODING
    previous_intent: TaskIntent | None = None
    current_goal: GoalSpec | None = None
    workflow_route: WorkflowRoute | None = None
    workflow_runs: dict[str, WorkflowRunState] = Field(default_factory=dict)
    recent_user_texts: list[str] = Field(default_factory=list)
    todo_state: TodoRunState | None = None

    def update_after_turn(
        self,
        resolution: GoalResolution,
        user_text: str,
        *,
        scope_text: str | None = None,
    ) -> None:
        del scope_text
        self.previous_intent = self.current_intent
        self.current_intent = resolution.intent.type
        if resolution.goal is not None:
            self.current_goal = resolution.goal
        elif resolution.intent.type == TaskIntent.GENERAL:
            self.current_goal = None
        self._record_user_text(user_text)
        self.workflow_route = _workflow_route_from_resolution(resolution)

    def set_goal(self, goal: GoalSpec | str | None) -> None:
        if goal is None:
            self.current_goal = None
            self._reset_workflow_context()
            return
        if isinstance(goal, GoalSpec):
            self.current_goal = goal
        else:
            self.current_goal = GoalSpec(type=infer_goal_type(goal), desc=goal)
        self.current_intent = TaskIntent.CODING
        self._reset_workflow_context()

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
            item
            for item in self.recent_user_texts[-(_INTENT_WINDOW_SIZE - 1):]
            if item
        ]
        parts = [*previous, current] if current else previous
        return _INTENT_WINDOW_SEPARATOR.join(parts[-_INTENT_WINDOW_SIZE:])

    def _record_user_text(self, text: str) -> None:
        item = _summarize_scope(text)
        if not item:
            return
        self.recent_user_texts = [*self.recent_user_texts, item][-_INTENT_WINDOW_SIZE:]


class ToolStatePatch(BaseModel):
    """Structured state updates requested by runtime tools."""

    intent: IntentResolution | None = None
    goal: GoalSpec | None = None
    persona: str | None = None
    workflow_runs: list[WorkflowRunState] = Field(default_factory=list)


# ── workflow route helpers ──────────────────────────────────────────


def _workflow_route_from_resolution(resolution: GoalResolution) -> WorkflowRoute | None:
    plan = resolution.plan
    if plan is None:
        return None
    return WorkflowRoute(join=plan.join, leave=plan.leave)


def _default_join_for_goal_type(goal_type: GoalType) -> str:
    return {
        GoalType.BUGFIX: "debug",
        GoalType.DEBUG: "debug",
        GoalType.REFACTOR: "brainstorm",
        GoalType.FEATURE: "brainstorm",
        GoalType.DESIGN: "brainstorm",
        GoalType.DOC: "design-doc",
        GoalType.REVIEW: "review",
        GoalType.CHORE: "tdd",
        GoalType.INSPECT: "",
    }.get(goal_type, "")


def _default_leave_for_goal_type(goal_type: GoalType) -> str | None:
    if goal_type in {GoalType.BUGFIX, GoalType.DEBUG, GoalType.REFACTOR, GoalType.FEATURE, GoalType.CHORE}:
        return "verify"
    if goal_type in {GoalType.DESIGN, GoalType.DOC, GoalType.REVIEW}:
        return _default_join_for_goal_type(goal_type)
    return None


# ── goal type inference ─────────────────────────────────────────────


def infer_goal_type(text: str) -> GoalType:
    normalized = text.lower()
    if _contains_any(normalized, _REVIEW_HINTS):
        return GoalType.REVIEW
    if _contains_any(normalized, _DEBUG_HINTS):
        return GoalType.DEBUG
    if _contains_any(normalized, _BUGFIX_HINTS) and _has_implementation_action_hint(normalized):
        return GoalType.BUGFIX
    if _contains_any(normalized, _REFACTOR_HINTS):
        return GoalType.REFACTOR
    if _contains_any(normalized, _DOC_HINTS):
        return GoalType.DOC
    if _contains_any(normalized, _DESIGN_HINTS):
        return GoalType.DESIGN
    if _contains_any(normalized, _FEATURE_HINTS):
        return GoalType.FEATURE
    if _contains_any(normalized, _INSPECT_HINTS):
        return GoalType.INSPECT
    if _contains_any(normalized, _WRITE_HINTS):
        return GoalType.FEATURE
    return GoalType.CHORE


def goal_label(goal: GoalSpec | dict | None) -> str:
    value = _coerce_goal(goal)
    return value.label if value is not None else ""


def goal_type_value(goal: GoalSpec | dict | None) -> str:
    value = _coerce_goal(goal)
    return value.type.value if value is not None else ""


# ── internal helpers ────────────────────────────────────────────────


def _coerce_goal(goal: GoalSpec | dict | None) -> GoalSpec | None:
    if goal is None:
        return None
    if isinstance(goal, GoalSpec):
        return goal
    if isinstance(goal, dict):
        try:
            return GoalSpec.model_validate(goal)
        except ValueError:
            return None
    return None


def _contains_any(text: str, hints: tuple[str, ...]) -> bool:
    return any(_contains_hint(text, hint) for hint in hints)


def _contains_hint(text: str, hint: str) -> bool:
    if hint.isascii() and re.fullmatch(r"[A-Za-z0-9_ -]+", hint):
        words = re.findall(r"[A-Za-z0-9_]+", hint)
        if len(words) == 1:
            return re.search(rf"(?<![A-Za-z0-9_]){re.escape(hint)}(?![A-Za-z0-9_])", text) is not None
    return hint in text


def _has_implementation_action_hint(text: str) -> bool:
    normalized = text.lower()
    return _contains_any(normalized, _WRITE_HINTS)


def _summarize_scope(text: str) -> str:
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    return first_line[:160]


__all__ = [
    "InteractionMode",
    "TaskIntent",
    "infer_task_intent",
    "GoalSpec",
    "IntentResolution",
    "PlanResolution",
    "GoalResolution",
    "GoalType",
    "WorkflowRoute",
    "TaskState",
    "TodoRunItem",
    "TodoRunState",
    "ToolStatePatch",
    "goal_label",
    "goal_type_value",
    "infer_goal_type",
]
