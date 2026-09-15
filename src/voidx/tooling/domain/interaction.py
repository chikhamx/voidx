"""User interaction values emitted by tool plugins."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator

from voidx.tooling.domain.ui_events import (
    CheckpointPlanPayload,
    ChoicePayload,
    GoalSpecPayload,
    LoopSpecPayload,
)


_NonBlank = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]


class InteractionPermissionTool(BaseModel):
    name: _NonBlank
    pattern: str = ""
    args: dict[str, Any] = Field(default_factory=dict)
    risk: dict[str, Any] | None = None
    allowed_scopes: list[_NonBlank] = Field(default_factory=list)
    default_scope: _NonBlank | None = None


class InteractionRequest(BaseModel):
    interaction_id: _NonBlank
    session_id: _NonBlank
    thread_id: _NonBlank
    turn_id: _NonBlank
    input_kind: Literal["choice", "text", "permission"]
    purpose: Literal["permission", "checkpoint", "clarify", "goal", "loop", "generic"]
    prompt: str
    choices: list[ChoicePayload] = Field(default_factory=list)
    allow_free_text: bool = False
    default_value: str = ""
    secret: bool = False
    timeout: float = Field(default=120.0, gt=0, allow_inf_nan=False)
    tools: list[InteractionPermissionTool] = Field(default_factory=list)
    allowed_scopes: list[_NonBlank] = Field(default_factory=list)
    checkpoint: CheckpointPlanPayload | None = None
    goal: GoalSpecPayload | None = None
    loop: LoopSpecPayload | None = None
    checkpoint_id: _NonBlank | None = None
    checkpoint_stage: Literal["decision", "scope"] | None = None

    @model_validator(mode="after")
    def validate_checkpoint_stage(self) -> InteractionRequest:
        if self.checkpoint_id is None and self.checkpoint_stage is None:
            return self
        if self.purpose != "checkpoint" or self.checkpoint_id is None or self.checkpoint_stage is None:
            raise ValueError("Checkpoint association requires checkpoint purpose, ID and stage")
        if self.checkpoint_stage == "decision":
            if self.input_kind != "choice" or self.interaction_id != self.checkpoint_id:
                raise ValueError("Checkpoint decision must be a choice with the checkpoint ID")
            values = [choice.value for choice in self.choices]
            if len(values) != len(set(values)) or not set(values) <= {"approved", "needs_doc", "modified", "rejected"}:
                raise ValueError("Checkpoint decision choices must be unique supported decisions")
        elif self.input_kind != "text" or self.interaction_id == self.checkpoint_id:
            raise ValueError("Checkpoint scope must be text with an independent ID")
        return self


class InteractionResolution(BaseModel):
    """An explicit policy result, separate from the external user response."""

    value: str = ""
    free_text: bool = False
    scope: _NonBlank | None = None
    decision: _NonBlank
    resolution_reason: Literal[
        "answered", "user_rejected", "dismissed", "timed_out", "task_cancelled"
    ]


class UserInteraction(BaseModel):
    prompt: str
    options: list[str | tuple[str, str, str]] = Field(default_factory=list)
    timeout: float | None = None


class UserResponse(BaseModel):
    value: str
    cancelled: bool = False
    free_text: bool = False


class InteractionResponse(UserResponse):
    """External answer carrying explicit run ownership and permission scope."""

    session_id: _NonBlank
    thread_id: _NonBlank
    turn_id: _NonBlank
    scope: _NonBlank | None = None
    rejected: bool = False


UserInteractionCallback = Callable[[UserInteraction], Awaitable[UserResponse]]


__all__ = [
    "InteractionPermissionTool",
    "InteractionRequest",
    "InteractionResolution",
    "InteractionResponse",
    "UserInteraction",
    "UserResponse",
    "UserInteractionCallback",
]
