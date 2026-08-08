"""Skill selection configuration owned by the skills domain."""

from pydantic import BaseModel, Field


class SkillSelectionConfig(BaseModel):
    enabled: set[str] = Field(default_factory=set)
    disabled: set[str] = Field(default_factory=set)
    auto: set[str] = Field(default_factory=set)
