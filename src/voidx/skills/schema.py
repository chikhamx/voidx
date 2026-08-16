"""Types for local skill discovery and selection."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from voidx.skills.domain.selection import SkillSelectionConfig

__all__ = [
    "SkillScope",
    "SkillMeta",
    "SkillDefinition",
    "SkillSelectionConfig",
    "SkillMatch",
]

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





class SkillMatch(BaseModel):
    skill: SkillDefinition
    reason: str

    @property
    def name(self) -> str:
        return self.skill.name
