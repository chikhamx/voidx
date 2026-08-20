"""Structured metadata for one runtime turn."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from voidx.agent.domain.turn_context import TurnExecutionContext


class TurnMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    profile_id: str = "coding"
    protocol: str = "turn"
    category: str = "coding"
    context: TurnExecutionContext | None = Field(default=None, exclude=True)


def turn_metadata_from_context(context: TurnExecutionContext) -> TurnMetadata:
    profile = context.runtime_profile
    return TurnMetadata(
        profile_id=profile.profile_id,
        protocol=profile.protocol,
        category=profile.profile_id,
        context=context,
    )
