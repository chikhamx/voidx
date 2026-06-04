"""Multi-turn task intent state."""

from __future__ import annotations

import re

from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel

from voidx.agent.runtime_context import (
    InteractionMode,
    TaskIntent,
    implementation_allowed_for_intent,
    infer_task_intent,
)


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


class TaskRun(BaseModel):
    goal: str = ""
    phase: TaskPhase = TaskPhase.CLARIFY
    status: TaskRunStatus = TaskRunStatus.IDLE
    approved_scope: str = ""
    awaiting_implementation_approval: bool = False
    turn_count: int = 0

    @property
    def active(self) -> bool:
        return self.status == TaskRunStatus.ACTIVE and bool(self.goal)

    def set_goal(self, goal: str) -> None:
        self.goal = _summarize_scope(goal)
        self.phase = TaskPhase.CLARIFY
        self.status = TaskRunStatus.ACTIVE if self.goal else TaskRunStatus.IDLE
        self.approved_scope = ""
        self.awaiting_implementation_approval = False
        self.turn_count = 0

    def clear(self) -> None:
        self.goal = ""
        self.phase = TaskPhase.CLARIFY
        self.status = TaskRunStatus.IDLE
        self.approved_scope = ""
        self.awaiting_implementation_approval = False
        self.turn_count = 0

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
        transition = _approval_transition(
            resolution.intent,
            _summarize_scope(scope_text or self.goal or user_text),
        )
        if transition.update:
            self.awaiting_implementation_approval = transition.awaiting_implementation_approval
            self.approved_scope = transition.approved_scope


class TaskState(BaseModel):
    current_intent: TaskIntent = TaskIntent.CHAT
    previous_intent: TaskIntent | None = None
    current_goal: str = ""
    awaiting_implementation_approval: bool = False
    approved_scope: str = ""
    last_plan_summary: str = ""

    def update_after_turn(
        self,
        resolution: "IntentResolution",
        user_text: str,
        *,
        scope_text: str | None = None,
    ) -> None:
        self.previous_intent = self.current_intent
        self.current_intent = resolution.intent
        self.current_goal = _summarize_scope(scope_text or user_text)
        transition = _approval_transition(resolution.intent, self.current_goal)
        if transition.update:
            self.awaiting_implementation_approval = transition.awaiting_implementation_approval
            self.approved_scope = transition.approved_scope
            if transition.last_plan_summary is not None:
                self.last_plan_summary = transition.last_plan_summary


class IntentResolution(BaseModel):
    intent: TaskIntent
    implementation_allowed: bool
    reason: str
    awaiting_implementation_approval: bool = False
    approved_scope: str = ""


@dataclass(frozen=True)
class _ApprovalTransition:
    update: bool
    awaiting_implementation_approval: bool = False
    approved_scope: str = ""
    last_plan_summary: str | None = None


def _approval_transition(intent: TaskIntent, scope: str) -> _ApprovalTransition:
    if intent == TaskIntent.DESIGN:
        return _ApprovalTransition(
            update=True,
            awaiting_implementation_approval=True,
            approved_scope=scope,
            last_plan_summary=scope,
        )
    if intent == TaskIntent.AMBIGUOUS:
        return _ApprovalTransition(update=False)
    return _ApprovalTransition(update=True)


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
        if state.awaiting_implementation_approval:
            return _resolution(
                TaskIntent.IMPLEMENT,
                "user confirmed the pending implementation plan",
                awaiting_implementation_approval=True,
                approved_scope=state.approved_scope,
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

    intent = infer_task_intent(text, mode)
    return _resolution(intent, f"single-turn classifier matched {intent.value}")


def _resolution(
    intent: TaskIntent,
    reason: str,
    *,
    awaiting_implementation_approval: bool = False,
    approved_scope: str = "",
) -> IntentResolution:
    return IntentResolution(
        intent=intent,
        implementation_allowed=implementation_allowed_for_intent(intent),
        reason=reason,
        awaiting_implementation_approval=awaiting_implementation_approval,
        approved_scope=approved_scope,
    )


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
