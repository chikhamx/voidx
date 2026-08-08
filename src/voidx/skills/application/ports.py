"""Ports used by skill application services."""

from typing import Protocol

from voidx.skills.schema import SkillDefinition


class SkillLookup(Protocol):
    def get(self, name: str) -> SkillDefinition | None: ...

    def is_enabled(self, skill: SkillDefinition) -> bool: ...
