"""Workspace-scoped skills application API."""

from __future__ import annotations

from voidx.skills.application.resolve_references import ResolveSkillReferences
from voidx.skills.domain.references import SkillReferenceMessage
from voidx.skills.service import SkillService


class SkillsApi:
    def __init__(self, service: SkillService) -> None:
        self.service = service
        self._resolve_references = ResolveSkillReferences(service)

    def resolve_references(self, user_text: str) -> SkillReferenceMessage:
        return self._resolve_references(user_text)
