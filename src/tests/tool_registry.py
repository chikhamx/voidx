"""Test composition for the explicit ToolRegistry plugin catalog."""

from __future__ import annotations

import os
from typing import Any

from voidx.agent.adapters.tools.plugins import build_agent_plugins
from voidx.tooling.adapters.mcp import McpGatewayTool
from voidx.tooling.adapters.lsp_post_edit import LspPostEditFormatter
from voidx.tooling.adapters.plugins import build_integration_plugins
from voidx.tooling.application.registry import ToolRegistry
from voidx.tooling.builtin.plugins import build_builtin_plugins
from voidx.bootstrap.tooling import TOOL_CAPABILITIES


def build_registry(
    *,
    settings: Any = None,
    tracker: Any = None,
    lsp_operations: Any = None,
    format_after_edit_enabled: bool = True,
) -> ToolRegistry:
    builtins = {
        plugin.id: plugin
        for plugin in build_builtin_plugins(
            formatter=(
                LspPostEditFormatter(lsp_operations, enabled=format_after_edit_enabled)
                if lsp_operations is not None
                else None
            )
        )
    }
    integrations = {
        plugin.id: plugin
        for plugin in build_integration_plugins(settings=settings, lsp_operations=lsp_operations)
    }
    agent_plugins = {
        plugin.id: plugin
        for plugin in build_agent_plugins(tracker=tracker)
    }
    integrations["mcp"] = McpGatewayTool(None)
    shell_id = "powershell" if os.name == "nt" else "bash"
    ordered_ids = [
        "read",
        "manage",
        "write",
        "replace",
        "git",
        "find",
        "search",
        "lsp",
        "lsp_format",
        "clarify",
        "checkpoint",
        "workflow",
        "compact",
        "document",
        "goal",
        "loop",
        shell_id,
        "todo",
        "skill",
        "webfetch",
        "websearch",
        "mcp",
    ]
    plugins = [
        (builtins | integrations | agent_plugins)[plugin_id]
        for plugin_id in ordered_ids
    ]
    return ToolRegistry(
        plugins, capabilities={plugin.id: TOOL_CAPABILITIES[plugin.id] for plugin in plugins}
    )


__all__ = ["build_registry"]
