"""Workspace-scoped skills composition."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from voidx.skills.application.api import SkillsApi
from voidx.skills.registry import SkillRegistry
from voidx.skills.service import SkillService

if TYPE_CHECKING:
    from voidx.config import Settings


def build_skills_api(workspace: str, settings: Settings | None) -> SkillsApi:
    selection = settings.get_skill_selection() if settings is not None else None
    return SkillsApi(
        SkillService(
            SkillRegistry(workspace),
            selection=selection,
        )
    )


class WorkspaceSkillsApiProvider:
    def __init__(self, base_workspace: str, base_settings: Settings | None) -> None:
        self._base_workspace = self._resolve(base_workspace)
        self._base_settings = base_settings
        self._cache: dict[str, SkillsApi] = {}

    @staticmethod
    def _resolve(workspace: str) -> str:
        return str(Path(workspace).resolve())

    def __call__(self, workspace: str) -> SkillsApi:
        resolved = self._resolve(workspace)
        cached = self._cache.get(resolved)
        if cached is not None:
            return cached
        if resolved == self._base_workspace:
            settings = self._base_settings
        else:
            from voidx.config import Settings

            settings = Settings(resolved)
        return self.replace(resolved, settings)

    def replace(self, workspace: str, settings: Settings | None) -> SkillsApi:
        resolved = self._resolve(workspace)
        if resolved == self._base_workspace:
            self._base_settings = settings
        api = build_skills_api(resolved, settings)
        self._cache[resolved] = api
        return api


def build_skills_api_provider(
    base_workspace: str,
    base_settings: Settings | None,
) -> WorkspaceSkillsApiProvider:
    return WorkspaceSkillsApiProvider(base_workspace, base_settings)
