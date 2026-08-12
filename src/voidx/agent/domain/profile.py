"""Runtime behavior profile descriptors."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from voidx.agent.domain.prompt_policy import ChatPromptPolicy, CodingPromptPolicy


class RuntimeProfile(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    profile_id: str
    revision: int = Field(ge=1)
    name: str
    # Graph-level protocol tool set for this profile: turn (default), loop, goal.
    protocol: str = "turn"
    system_prompt: str = ""
    constraints: tuple[str, ...] = ()
    persona: str | None = None
    continuation_policy: dict[str, Any] = Field(default_factory=dict)
    # Optional prompt injection policy. ``None`` (default) keeps the standard
    # coding prompt sections. Chat and future profiles supply a PromptPolicy
    # implementation to suppress or override sections.
    prompt_policy: Any | None = None

    @field_serializer("prompt_policy")
    def serialize_prompt_policy(self, policy: Any) -> str | None:
        # PromptPolicy is a runtime-only object; persist its type name so every
        # dump path (json mode or not) is serialization-safe.
        return type(policy).__name__ if policy is not None else None

    @field_validator("profile_id", "name")
    @classmethod
    def require_value(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value


CODING_PROFILE = RuntimeProfile(
    profile_id="coding", revision=1, name="Coding", prompt_policy=CodingPromptPolicy()
)
CHAT_PROFILE = RuntimeProfile(
    profile_id="chat", revision=1, name="Chat", prompt_policy=ChatPromptPolicy()
)
