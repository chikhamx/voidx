"""Explicit LangGraph execution composition for tests."""

from __future__ import annotations

from voidx.agent.infrastructure.langgraph.execution import LangGraphExecution
from tests.presentation_ui import make_presentation_ui
from voidx.agent.ports.workspace_lock import NullWorkspaceWriteLock


def make_langgraph_execution(*args, ui=None, **kwargs) -> LangGraphExecution:
    if "skills_api" not in kwargs:
        from voidx.skills.application.api import SkillsApi
        from voidx.skills.registry import SkillRegistry
        from voidx.skills.service import SkillService

        workspace = str(getattr(args[0], "workspace", ".")) if args else "."
        api = SkillsApi(SkillService(SkillRegistry(workspace)))
        kwargs["skills_api"] = api
        kwargs["skills_api_provider"] = lambda _workspace: api

    return LangGraphExecution(
        *args,
        ui=ui or make_presentation_ui(),
        workspace_write_lock=kwargs.pop("workspace_write_lock", NullWorkspaceWriteLock()),
        **kwargs,
    )
