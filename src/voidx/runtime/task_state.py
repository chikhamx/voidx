"""Multi-turn task intent state shared across runtime layers."""

from __future__ import annotations

import re

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from voidx.runtime.intent import InteractionMode, TaskIntent, infer_task_intent
from voidx.runtime.intent_classifier import IntentClassifierResult, classify_intent
from voidx.workflow.runtime import WorkflowRunState


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

_DIRECT_IMPLEMENT_COMMANDS = {
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


class TaskPhase(str, Enum):
    CLARIFY = "clarify"
    INSPECT = "inspect"
    DESIGN = "design"
    IMPLEMENT = "implement"
    VERIFY = "verify"
    REVIEW = "review"
    DONE = "done"


class TaskRunStatus(str, Enum):
    IDLE = "idle"
    ACTIVE = "active"
    DONE = "done"


class PendingApproval(BaseModel):
    kind: Literal["implementation"] = "implementation"
    scope: str
    source_intent: TaskIntent = TaskIntent.DESIGN
    created_turn: int = 0


class TaskRun(BaseModel):
    goal: str = ""
    phase: TaskPhase = TaskPhase.CLARIFY
    status: TaskRunStatus = TaskRunStatus.IDLE
    pending_approval: PendingApproval | None = None
    turn_count: int = 0
    skill_runs: dict[str, WorkflowRunState] = Field(default_factory=dict)

    @property
    def active(self) -> bool:
        return self.status == TaskRunStatus.ACTIVE and bool(self.goal)

    def set_goal(self, goal: str) -> None:
        self.goal = _summarize_scope(goal)
        self.phase = TaskPhase.CLARIFY
        self.status = TaskRunStatus.ACTIVE if self.goal else TaskRunStatus.IDLE
        self.pending_approval = None
        self.turn_count = 0
        self.skill_runs = {}

    def clear(self) -> None:
        self.goal = ""
        self.phase = TaskPhase.CLARIFY
        self.status = TaskRunStatus.IDLE
        self.pending_approval = None
        self.turn_count = 0
        self.skill_runs = {}

    def merge_skill_runs(self, runs: list[WorkflowRunState | dict]) -> None:
        for item in runs:
            run = item if isinstance(item, WorkflowRunState) else WorkflowRunState.model_validate(item)
            self.skill_runs[run.name] = run

    def update_after_turn(
        self,
        resolution: "IntentResolution",
        user_text: str,
        *,
        scope_text: str | None = None,
    ) -> None:
        if not self.goal:
            self.set_goal(scope_text or user_text)
        if not self.goal:
            return

        self.status = TaskRunStatus.ACTIVE
        self.turn_count += 1
        self.phase = _phase_for_intent(resolution.intent)
        if resolution.intent == TaskIntent.AMBIGUOUS:
            return
        self.pending_approval = _next_pending_approval(
            resolution,
            resolution.intent,
            _summarize_scope(scope_text or self.goal or user_text),
            turn_count=self.turn_count,
        )


class TaskState(BaseModel):
    current_intent: TaskIntent = TaskIntent.CHAT
    previous_intent: TaskIntent | None = None
    current_goal: str = ""
    pending_approval: PendingApproval | None = None
    last_plan_summary: str = ""
    recent_user_texts: list[str] = Field(default_factory=list)

    def update_after_turn(
        self,
        resolution: "IntentResolution",
        user_text: str,
        *,
        scope_text: str | None = None,
    ) -> None:
        self.previous_intent = self.current_intent
        self.current_intent = resolution.intent
        goal_text = scope_text or (
            resolution.confirmed_approval.scope
            if resolution.confirmed_approval
            else user_text
        )
        self.current_goal = _summarize_scope(goal_text)
        self._record_user_text(user_text)
        if resolution.intent == TaskIntent.AMBIGUOUS:
            return
        if resolution.intent == TaskIntent.DESIGN:
            self.last_plan_summary = self.current_goal
        self.pending_approval = _next_pending_approval(
            resolution,
            resolution.intent,
            self.current_goal,
        )

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


class IntentResolution(BaseModel):
    intent: TaskIntent
    reason: str
    confirmed_approval: PendingApproval | None = None


class ToolStatePatch(BaseModel):
    """Structured state updates requested by runtime tools."""
    task_intent: TaskIntent | None = None
    intent_resolution_reason: str | None = None
    goal: str | None = None
    goal_phase: str | None = None
    goal_status: str | None = None
    pending_approval: PendingApproval | None = None
    available_tool_ids: list[str] | None = None
    skill_runs: list[WorkflowRunState] = Field(default_factory=list)
    intent_confidence: float | None = None
    intent_source: str | None = None
    intent_refined: bool | None = None


def resolve_turn_intent(
    text: str,
    interaction_mode: str | InteractionMode | None = None,
    task_state: TaskState | None = None,
) -> IntentResolution:
    mode = InteractionMode.parse(interaction_mode)
    state = task_state or TaskState()

    if mode == InteractionMode.PLAN:
        return _resolution(TaskIntent.DESIGN, "interaction mode forces design")

    if _is_approval_only(text):
        if state.pending_approval:
            return _resolution(
                TaskIntent.IMPLEMENT,
                "user confirmed the pending implementation plan",
                confirmed_approval=state.pending_approval,
            )
        return _resolution(
            TaskIntent.AMBIGUOUS,
            "approval phrase without a pending implementation plan",
        )

    if _is_direct_implementation_command(text):
        return _resolution(
            TaskIntent.IMPLEMENT,
            "direct short command asks to modify the current task",
        )

    classification = classify_intent(
        text,
        mode,
        classifier_text=state.intent_window_text(text),
    )
    if classification is not None:
        if classification.action == "accept":
            return _resolution(
                classification.intent,
                _intent_classifier_reason(classification),
            )
        if classification.action == "suggest" and classification.intent == TaskIntent.IMPLEMENT:
            return _resolution(
                TaskIntent.AMBIGUOUS,
                f"local classifier suggested implement confidence={classification.confidence:.2f}; confirmation required",
            )

    intent = infer_task_intent(text, mode)
    return _resolution(intent, f"keyword classifier matched {intent.value}")


def _next_pending_approval(
    resolution: IntentResolution,
    intent: TaskIntent,
    scope: str,
    *,
    turn_count: int = 0,
) -> PendingApproval | None:
    if intent == TaskIntent.DESIGN:
        return PendingApproval(
            scope=scope,
            source_intent=TaskIntent.DESIGN,
            created_turn=turn_count,
        )
    return None


def _resolution(
    intent: TaskIntent,
    reason: str,
    *,
    confirmed_approval: PendingApproval | None = None,
) -> IntentResolution:
    return IntentResolution(
        intent=intent,
        reason=reason,
        confirmed_approval=confirmed_approval,
    )


def _intent_classifier_reason(result: IntentClassifierResult) -> str:
    if result.source == "keyword_classifier":
        return f"keyword classifier matched {result.intent.value}"
    return f"local classifier matched {result.intent.value} confidence={result.confidence:.2f}"


def _is_approval_only(text: str) -> bool:
    return _normalize_approval_text(text) in _APPROVAL_ONLY_HINTS


def _is_direct_implementation_command(text: str) -> bool:
    return _normalize_approval_text(text) in _DIRECT_IMPLEMENT_COMMANDS


def _normalize_approval_text(text: str) -> str:
    return re.sub(r"[\s,.;:!?，。；：！？、]+", "", text.strip().lower())


def _summarize_scope(text: str) -> str:
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    return first_line[:160]


def _phase_for_intent(intent: TaskIntent) -> TaskPhase:
    if intent == TaskIntent.INSPECT:
        return TaskPhase.INSPECT
    if intent == TaskIntent.DESIGN:
        return TaskPhase.DESIGN
    if intent == TaskIntent.IMPLEMENT:
        return TaskPhase.IMPLEMENT
    if intent == TaskIntent.REVIEW:
        return TaskPhase.REVIEW
    if intent == TaskIntent.DEBUG:
        return TaskPhase.INSPECT
    return TaskPhase.CLARIFY


__all__ = [
    "InteractionMode",
    "TaskIntent",
    "infer_task_intent",
    "IntentResolution",
    "PendingApproval",
    "TaskPhase",
    "TaskRun",
    "TaskRunStatus",
    "TaskState",
    "ToolStatePatch",
    "resolve_turn_intent",
]
