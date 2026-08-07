"""Explicit factory for Tooling integration plugins."""

from __future__ import annotations

from typing import Any

from voidx.lsp.ports.operations import LspOperations
from voidx.tooling.adapters.lsp import LspFormatTool, LspTool
from voidx.tooling.adapters.skills import SkillsTool
from voidx.tooling.application.execution import AuthorizationRuntime
from voidx.tooling.domain.file_tracking import FileStateStore
from voidx.tooling.adapters.scoped_plugin import FileScopedPlugin
from voidx.tooling.builtin.web import WebFetchTool, WebSearchTool
from voidx.tooling.ports.tool import ToolPlugin
from voidx.tooling.ports.web_route import WebRoute


def build_integration_plugins(
    *,
    settings: Any = None,
    web_route: WebRoute | None = None,
    lsp_operations: LspOperations | None = None,
    authorization: AuthorizationRuntime | None = None,
    files: FileStateStore | None = None,
) -> list[ToolPlugin]:
    retry_config = settings.get_retry_config() if settings is not None else None
    authorization = authorization or AuthorizationRuntime()
    files = files or FileStateStore()
    return [
        FileScopedPlugin(LspTool(lsp_operations, authorization), authorization, files),
        FileScopedPlugin(LspFormatTool(lsp_operations), authorization, files),
        SkillsTool(settings=settings),
        WebFetchTool(settings=settings, retry_config=retry_config, web_route=web_route),
        WebSearchTool(settings=settings, web_route=web_route),
    ]


__all__ = ["build_integration_plugins"]
