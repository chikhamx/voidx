"""Types for local skill discovery and selection."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


SkillScope = Literal["bundled", "global", "project"]


class SkillMeta(BaseModel):
    name: str
    description: str = ""
    enabled: bool = True
    triggers: list[str] = Field(default_factory=list)
    scope: SkillScope = "project"


class SkillDefinition(BaseModel):
    meta: SkillMeta
    path: Path
    body: str

    @property
    def name(self) -> str:
        return self.meta.name

    @property
    def source_dir(self) -> Path:
        return self.path.parent


class SkillSelectionConfig(BaseModel):
    enabled: set[str] = Field(default_factory=set)
    disabled: set[str] = Field(default_factory=set)


class SkillMatch(BaseModel):
    skill: SkillDefinition
    reason: str

    @property
    def name(self) -> str:
        return self.skill.name
