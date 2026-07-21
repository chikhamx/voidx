"""Runtime behavior profile descriptors."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RuntimeProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    profile_id: str
    revision: int = Field(ge=1)
    name: str

    @field_validator("profile_id", "name")
    @classmethod
    def require_value(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value
