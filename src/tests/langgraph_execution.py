"""Explicit LangGraph execution composition for tests."""

from __future__ import annotations

from voidx.agent.infrastructure.langgraph.execution import LangGraphExecution
from tests.presentation_ui import make_presentation_ui
from voidx.agent.ports.workspace_lock import NullWorkspaceWriteLock


def make_langgraph_execution(*args, ui=None, **kwargs) -> LangGraphExecution:
    return LangGraphExecution(
        *args,
        ui=ui or make_presentation_ui(),
        workspace_write_lock=kwargs.pop("workspace_write_lock", NullWorkspaceWriteLock()),
        **kwargs,
    )
