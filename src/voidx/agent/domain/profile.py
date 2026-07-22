"""Runtime behavior profile descriptors."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RuntimeProfile(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    profile_id: str
    revision: int = Field(ge=1)
    name: str
    # Optional prompt injection policy. ``None`` (default) keeps the standard
    # coding prompt sections. Chat and future profiles supply a PromptPolicy
    # implementation to suppress or override sections.
    prompt_policy: Any | None = None

    @field_validator("profile_id", "name")
    @classmethod
    def require_value(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value
