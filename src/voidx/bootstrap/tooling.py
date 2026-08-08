"""Tooling and integration adapter composition."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from voidx.agent.adapters.tools.plugins import build_agent_plugins
from voidx.agent.application.runtime.task_tracker import TaskTracker
from voidx.agent.application.agents import child_agent_descriptions_for_llm, get_agent, get_subagents
from voidx.config import Config, Settings
from voidx.tooling.adapters.mcp import McpGatewayTool
from voidx.tooling.adapters.plugins import build_integration_plugins
from voidx.tooling.application.registry import ToolRegistry
from voidx.tooling.builtin.plugins import build_builtin_plugins

_CATALOG_ORDER = (
    "read", "manage", "write", "replace", "git", "find", "search",
    "lsp", "lsp_format", "clarify", "checkpoint", "workflow", "compact",
    "document", "goal", "loop", "powershell" if os.name == "nt" else "bash",
    "todo", "skill", "webfetch", "websearch", "mcp", "agent", "agent_control",
)


def _order_catalog_plugins(plugins: list[Any]) -> list[Any]:
    by_id: dict[str, Any] = {}
    for plugin in plugins:
        if plugin.id in by_id:
            raise ValueError(f"Duplicate tool id: {plugin.id}")
        by_id[plugin.id] = plugin
    missing = [tool_id for tool_id in _CATALOG_ORDER if tool_id not in by_id]
    unexpected = sorted(set(by_id) - set(_CATALOG_ORDER))
    if missing or unexpected:
        raise ValueError(f"Invalid tool catalog: missing={missing}, unexpected={unexpected}")
    return [by_id[tool_id] for tool_id in _CATALOG_ORDER]


def build_tool_registry(
    *,
    settings: Settings | None,
    config: Config,
    subagent_runner: Callable[..., Any],
    web_route: Callable[..., Any] | None = None,
) -> tuple[TaskTracker, ToolRegistry]:
    tracker = TaskTracker()
    plugins = [
        *build_builtin_plugins(),
        *build_integration_plugins(settings=settings, web_route=web_route),
        McpGatewayTool(None),
        *build_agent_plugins(
            tracker=tracker,
            subagent_runner=subagent_runner,
            agent_resolver=get_agent,
            child_agent_descriptions=child_agent_descriptions_for_llm(),
            available_agents=[agent.name for agent in get_subagents()],
        ),
    ]
    return tracker, ToolRegistry(_order_catalog_plugins(plugins))


def register_agent_tool(
    registry: ToolRegistry,
    *,
    config: Config,
    subagent_runner: Callable[..., Any],
) -> None:
    registry.unregister_prefix("agent")
    for plugin in build_agent_plugins(
        tracker=None,
        subagent_runner=subagent_runner,
        agent_resolver=get_agent,
        child_agent_descriptions=child_agent_descriptions_for_llm(),
        available_agents=[agent.name for agent in get_subagents()],
    ):
        if plugin.id in {"agent", "agent_control"}:
            registry.register_plugin(plugin)


def apply_mcp_tool_denials(configs: list[Any], permission: Any) -> None:
    for server in configs:
        if not isinstance(server.tools, dict):
            continue
        for tool_name, allowed in server.tools.items():
            if not allowed:
                permission.deny_silent(f"mcp@pattern:mcp:{server.name}:{tool_name}")


def build_external_managers(
    *,
    settings: Any | None,
    tools: ToolRegistry,
    permission: Any,
    workspace: str,
    model: Any | None = None,
    model_config: Any | None = None,
) -> tuple[Any, Any]:
    from voidx.llm.adapters.langchain_model_factory import create_resolver_model
    from voidx.lsp.adapters.client import create_lsp_client
    from voidx.lsp.application.manager import LspManager
    from voidx.lsp.application.service import LspOperationsService
    from voidx.mcp.adapters.client import create_mcp_client
    from voidx.mcp.application.manager import McpManager
    from voidx.tooling.adapters.mcp_description_generator import McpDescriptionGenerator

    description_model = create_resolver_model(model, model_config) if model is not None and model_config is not None else model
    configs = settings.list_mcp_servers() if settings is not None else []
    apply_mcp_tool_denials(configs, permission)
    mcp_manager = McpManager(
        configs,
        create_mcp_client,
        description_generator=McpDescriptionGenerator(description_model).generate,
        workspace=workspace,
    )
    gateway_tool = McpGatewayTool(mcp_manager)
    tools.replace(gateway_tool.id, gateway_tool, gateway_tool.description, gateway_tool.parameters_schema())
    lsp_manager = LspManager(workspace, create_lsp_client)
    lsp_operations = LspOperationsService(lsp_manager)
    from voidx.tooling.adapters.lsp import LspFormatTool, LspTool
    from voidx.tooling.application.execution import AuthorizationRuntime
    from voidx.tooling.domain.file_tracking import FileStateStore
    from voidx.tooling.adapters.scoped_plugin import FileScopedPlugin

    authorization = AuthorizationRuntime()
    files = FileStateStore()
    for plugin in (
        FileScopedPlugin(LspTool(lsp_operations, authorization), authorization, files),
        FileScopedPlugin(LspFormatTool(lsp_operations), authorization, files),
    ):
        tools.replace(plugin.id, plugin, plugin.description, plugin.parameters_schema())
    return mcp_manager, lsp_manager


async def resolve_mcp_references(user_text: str, *, settings: Any, manager: Any) -> Any:
    from voidx.agent.adapters.mcp.references import mcp_reference_message

    return await mcp_reference_message(user_text, settings=settings, manager=manager)


__all__ = [
    "build_external_managers",
    "build_tool_registry",
    "register_agent_tool",
    "resolve_mcp_references",
]
