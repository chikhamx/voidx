"""Multi-turn task state shared across runtime layers."""

from __future__ import annotations

import re

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from voidx.runtime.intent import InteractionMode, TaskIntent, infer_task_intent
from voidx.workflow.types import WorkflowRunState


_INTENT_WINDOW_SIZE = 2
_INTENT_WINDOW_SEPARATOR = " [SEP] "


_APPROVAL_ONLY_HINTS = {
    "ok",
    "okay",
    "yes",
    "yeah",
    "yep",
    "sure",
    "confirm",
    "confirmed",
    "approve",
    "goahead",
    "go",
    "yesgo",
    "\u597d",
    "\u597d\u7684",
    "\u884c",
    "\u53ef\u4ee5",
    "\u53ef\u4ee5\u7684",
    "\u53ef\u4ee5\u4e86",
    "\u5bf9\u53ef\u4ee5",
    "\u5bf9\u7684",
    "\u5bf9",
    "\u55ef",
    "\u786e\u8ba4",
    "\u786e\u8ba4\u4e86",
    "\u662f\u7684",
    "\u540c\u610f",
    "\u6279\u51c6",
    "\u6ca1\u95ee\u9898",
    "\u6ca1\u95ee\u9898\u4e86",
    "\u5f00\u59cb\u5427",
    "\u505a\u5427",
    "\u6309\u8fd9\u4e2a\u505a",
    "\u5c31\u8fd9\u6837",
}

_DIRECT_WRITE_COMMANDS = {
    "change",
    "edit",
    "fix",
    "implement",
    "modify",
    "patch",
    "doit",
    "goaheadandchange",
    "\u6539",
    "\u6539\u5427",
    "\u4fee\u6539",
    "\u4fee\u6539\u5427",
    "\u4fee",
    "\u4fee\u5427",
    "\u4fee\u590d",
    "\u4fee\u590d\u5427",
    "\u5b9e\u73b0",
    "\u5b9e\u73b0\u5427",
    "\u505a",
    "\u505a\u5427",
    "\u5f00\u59cb\u6539",
    "\u5f00\u59cb\u505a",
}

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


class Goal(BaseModel):
    type: GoalType
    target: str = ""
    expected_result: str = ""
    user_requested_write: bool = False
    needs_confirmation: bool = False

    @property
    def label(self) -> str:
        target = self.target.strip()
        return target or self.expected_result.strip() or self.type.value


class GoalResolution(BaseModel):
    intent: TaskIntent = TaskIntent.CODING
    goal: Goal | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)
    reason: str = ""
    workflow_start: str | None = None
    workflow_end: str | None = None


class PendingApproval(BaseModel):
    kind: Literal["implementation"] = "implementation"
    scope: str
    source_goal_type: GoalType = GoalType.DESIGN
    created_turn: int = 0


class WorkflowRoute(BaseModel):
    start: str = ""
    end: str = ""


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
    current_goal: Goal | None = None
    pending_approval: PendingApproval | None = None
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
        self.current_intent = resolution.intent
        if resolution.goal is not None:
            self.current_goal = resolution.goal
        elif resolution.intent == TaskIntent.GENERAL:
            self.current_goal = None
        self._record_user_text(user_text)
        self.pending_approval = _next_pending_approval(resolution, self.current_goal)
        self.workflow_route = _workflow_route_from_resolution(resolution, self.current_goal)

    def set_goal(self, goal: Goal | str | None) -> None:
        if goal is None:
            self.current_goal = None
            self._reset_workflow_context()
            return
        self.current_goal = goal if isinstance(goal, Goal) else goal_from_text(goal)
        self.current_intent = TaskIntent.CODING
        self._reset_workflow_context()

    def _reset_workflow_context(self) -> None:
        self.pending_approval = None
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


IntentResolution = GoalResolution


class ToolStatePatch(BaseModel):
    """Structured state updates requested by runtime tools."""

    task_intent: TaskIntent | None = None
    goal: Goal | None = None
    pending_approval: PendingApproval | None = None
    persona: str | None = None
    workflow_runs: list[WorkflowRunState] = Field(default_factory=list)


def resolve_turn_intent(
    text: str,
    interaction_mode: str | InteractionMode | None = None,
    task_state: TaskState | None = None,
) -> GoalResolution:
    mode = InteractionMode.parse(interaction_mode)
    state = task_state or TaskState()

    if mode == InteractionMode.PLAN:
        return _resolution(TaskIntent.CODING, "interaction mode forces coding")

    if _is_approval_only(text):
        if state.pending_approval:
            goal_type = (
                GoalType.FEATURE
                if state.pending_approval.source_goal_type == GoalType.DESIGN
                else state.pending_approval.source_goal_type
            )
            return _resolution(
                TaskIntent.CODING,
                "user confirmed the pending implementation plan",
                goal=goal_from_text(
                    state.pending_approval.scope,
                    goal_type=goal_type,
                    user_requested_write=True,
                    needs_confirmation=False,
                ),
            )
        return _resolution(
            TaskIntent.GENERAL,
            "approval phrase without a pending implementation plan",
            confidence=0.6,
        )

    if _is_direct_write_command(text):
        return _resolution(
            TaskIntent.CODING,
            "direct short command asks to modify the current task",
        )

    if mode == InteractionMode.GOAL and state.current_goal is not None:
        return _resolution(
            TaskIntent.CODING,
            "goal mode keeps the turn scoped to the current goal",
        )

    intent = infer_task_intent(text, mode)
    return _resolution(intent, f"local classifier matched {intent.value}")


def goal_from_text(
    text: str,
    *,
    goal_type: GoalType | str | None = None,
    user_requested_write: bool | None = None,
    needs_confirmation: bool | None = None,
    expected_result: str = "",
) -> Goal:
    normalized_type = GoalType(goal_type) if goal_type is not None else infer_goal_type(text)
    target = _summarize_scope(text)
    requested_write = _user_requested_write(text) if user_requested_write is None else user_requested_write
    confirmation = (
        _needs_confirmation(normalized_type, requested_write)
        if needs_confirmation is None
        else needs_confirmation
    )
    return Goal(
        type=normalized_type,
        target=target,
        expected_result=expected_result,
        user_requested_write=requested_write,
        needs_confirmation=confirmation,
    )


def infer_goal_type(text: str) -> GoalType:
    normalized = text.lower()
    if _contains_any(normalized, _REVIEW_HINTS):
        return GoalType.REVIEW
    if _contains_any(normalized, _DEBUG_HINTS):
        return GoalType.DEBUG
    if _contains_any(normalized, _BUGFIX_HINTS) and _user_requested_write(normalized):
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


def goal_label(goal: Goal | dict | None) -> str:
    value = _coerce_goal(goal)
    return value.label if value is not None else ""


def goal_type_value(goal: Goal | dict | None) -> str:
    value = _coerce_goal(goal)
    return value.type.value if value is not None else ""


def _next_pending_approval(
    resolution: GoalResolution,
    goal: Goal | None,
) -> PendingApproval | None:
    del resolution
    if goal is not None and goal.type == GoalType.DESIGN and goal.needs_confirmation:
        return PendingApproval(
            scope=goal.label,
            source_goal_type=goal.type,
        )
    return None


def _workflow_route_from_resolution(
    resolution: GoalResolution,
    goal: Goal | None,
) -> WorkflowRoute | None:
    start = (resolution.workflow_start or "").strip().lower()
    end = (resolution.workflow_end or "").strip().lower()
    if not start:
        start = _workflow_start_for_goal(goal)
    if not end:
        end = default_workflow_end_for_goal(goal, start)
    if not start and not end:
        return None
    return WorkflowRoute(start=start, end=end)


def _workflow_start_for_goal(goal: Goal | None) -> str:
    if goal is None:
        return ""
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
    }.get(goal.type, "")


def default_workflow_end_for_goal(goal: Goal | None, start: str) -> str:
    if goal is not None:
        if goal.type == GoalType.REVIEW and goal.user_requested_write:
            return "verify"
        if start == "tdd" and goal.user_requested_write:
            return "verify"
    return start


def _resolution(
    intent: TaskIntent,
    reason: str,
    *,
    goal: Goal | None = None,
    confidence: float = 1.0,
) -> GoalResolution:
    return GoalResolution(
        intent=intent,
        goal=goal,
        confidence=confidence,
        reason=reason,
    )


def _coerce_goal(goal: Goal | dict | None) -> Goal | None:
    if goal is None:
        return None
    if isinstance(goal, Goal):
        return goal
    if isinstance(goal, dict):
        try:
            return Goal.model_validate(goal)
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


def _needs_confirmation(goal_type: GoalType, user_requested_write: bool) -> bool:
    return goal_type == GoalType.DESIGN and not user_requested_write


def _user_requested_write(text: str) -> bool:
    normalized = text.lower()
    return _contains_any(normalized, _WRITE_HINTS)


def _is_approval_only(text: str) -> bool:
    return _normalize_approval_text(text) in _APPROVAL_ONLY_HINTS


def _is_direct_write_command(text: str) -> bool:
    return _normalize_approval_text(text) in _DIRECT_WRITE_COMMANDS


def _normalize_approval_text(text: str) -> str:
    return re.sub(r"[\s,.;:!?，。；：！？、]+", "", text.strip().lower())


def _summarize_scope(text: str) -> str:
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    return first_line[:160]


__all__ = [
    "InteractionMode",
    "TaskIntent",
    "infer_task_intent",
    "Goal",
    "GoalResolution",
    "GoalType",
    "IntentResolution",
    "PendingApproval",
    "WorkflowRoute",
    "TaskState",
    "TodoRunItem",
    "TodoRunState",
    "ToolStatePatch",
    "default_workflow_end_for_goal",
    "goal_from_text",
    "goal_label",
    "goal_type_value",
    "infer_goal_type",
    "resolve_turn_intent",
]
