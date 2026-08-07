"""Presentation-agnostic UI event DTOs and publisher port for tools."""

from __future__ import annotations

from typing import Literal, Protocol, TypeAlias

from pydantic import BaseModel, ConfigDict, Field


class ToolUiEventBase(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: str


class ChoicePayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    value: str
    description: str = ""


class CheckpointPlanPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    goal: str
    steps: list[str] = Field(default_factory=list)
    affected_files: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class CheckpointPromptShown(ToolUiEventBase):
    kind: Literal["checkpoint_prompt.shown"] = "checkpoint_prompt.shown"
    checkpoint_id: str
    plan: CheckpointPlanPayload
    choices: list[ChoicePayload] = Field(default_factory=list)


class CheckpointDecisionSubmitted(ToolUiEventBase):
    kind: Literal["checkpoint_decision.submitted"] = "checkpoint_decision.submitted"
    checkpoint_id: str
    decision: str
    label: str = ""
    response: str = ""
    was_custom_input: bool = False


class GoalSpecPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    objective: str
    acceptance_condition: str
    achievement_method: str = ""
    max_attempts: int = 20


class GoalSpecPromptShown(ToolUiEventBase):
    kind: Literal["goal_spec_prompt.shown"] = "goal_spec_prompt.shown"
    prompt_id: str
    spec: GoalSpecPayload
    choices: list[ChoicePayload] = Field(default_factory=list)


class GoalSpecDecisionSubmitted(ToolUiEventBase):
    kind: Literal["goal_spec_decision.submitted"] = "goal_spec_decision.submitted"
    prompt_id: str
    decision: str
    response: str = ""


class ClarifyPromptShown(ToolUiEventBase):
    kind: Literal["clarify_prompt.shown"] = "clarify_prompt.shown"
    clarify_id: str
    question: str
    options: list[str] = Field(default_factory=list)


class ClarifyAnswerSubmitted(ToolUiEventBase):
    kind: Literal["clarify_answer.submitted"] = "clarify_answer.submitted"
    clarify_id: str
    answer: str
    cancelled: bool = False
    was_custom_input: bool = True


ToolUiEvent: TypeAlias = (
    CheckpointPromptShown
    | CheckpointDecisionSubmitted
    | GoalSpecPromptShown
    | GoalSpecDecisionSubmitted
    | ClarifyPromptShown
    | ClarifyAnswerSubmitted
)


class ToolUiEventPublisher(Protocol):
    @property
    def is_running(self) -> bool: ...

    def emit(self, event: ToolUiEvent) -> None: ...
