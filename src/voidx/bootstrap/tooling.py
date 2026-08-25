"""Tooling and integration adapter composition."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from voidx.agent.adapters.tools.plugins import build_agent_plugins
from voidx.agent.application.runtime.task_tracker import TaskTracker
from voidx.agent.application.agents import child_agent_descriptions_for_llm, get_agent, get_subagents
from voidx.agent.domain.tool_policy import ProfileToolPolicy
from voidx.config import Config, Settings
from voidx.tooling.adapters.mcp import McpGatewayTool
from voidx.tooling.adapters.skills import ReadOnlySkillsTool
from voidx.tooling.adapters.plugins import build_integration_plugins
from voidx.tooling.application.registry import ToolRegistry
from voidx.tooling.builtin.plugins import build_builtin_plugins
from voidx.tooling.domain.capability import ToolCapability


TOOL_CAPABILITIES = {
    "read": ToolCapability.READ_ONLY,
    "manage": ToolCapability.EXECUTION_GATED,
    "write": ToolCapability.EXECUTION_GATED,
    "replace": ToolCapability.EXECUTION_GATED,
    "git": ToolCapability.EXECUTION_GATED,
    "find": ToolCapability.READ_ONLY,
    "search": ToolCapability.READ_ONLY,
    "lsp": ToolCapability.READ_ONLY,
    "lsp_format": ToolCapability.EXECUTION_GATED,
    "clarify": ToolCapability.HITL_INTERACTION,
    "checkpoint": ToolCapability.HITL_INTERACTION,
    "workflow": ToolCapability.ORCHESTRATION,
    "compact": ToolCapability.ORCHESTRATION,
    "document": ToolCapability.READ_ONLY,
    "goal_init": ToolCapability.ORCHESTRATION,
    "goal_checkpoint": ToolCapability.ORCHESTRATION,
    "goal_decision": ToolCapability.ORCHESTRATION,
    "loop": ToolCapability.ORCHESTRATION,
    "powershell" if os.name == "nt" else "bash": ToolCapability.EXECUTION_GATED,
    "todo": ToolCapability.ORCHESTRATION,
    "skill": ToolCapability.ORCHESTRATION,
    "webfetch": ToolCapability.READ_ONLY,
    "websearch": ToolCapability.READ_ONLY,
    "mcp": ToolCapability.EXTERNAL,
    "agent": ToolCapability.ORCHESTRATION,
    "agent_control": ToolCapability.ORCHESTRATION,
}

_CATALOG_ORDER = (
    "read", "manage", "write", "replace", "git", "find", "search",
    "lsp", "lsp_format", "clarify", "checkpoint", "workflow", "compact",
    "document", "goal_init", "goal_checkpoint", "goal_decision", "loop",
    "powershell" if os.name == "nt" else "bash",
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
    skills_api_provider: Any,
    web_route: Callable[..., Any] | None = None,
) -> tuple[TaskTracker, ToolRegistry]:
    tracker = TaskTracker()
    plugins = [
        *build_builtin_plugins(),
        *build_integration_plugins(
            settings=settings,
            skills_api_provider=skills_api_provider,
            web_route=web_route,
        ),
        McpGatewayTool(None),
        *build_agent_plugins(
            tracker=tracker,
            subagent_runner=subagent_runner,
            agent_resolver=get_agent,
            child_agent_descriptions=child_agent_descriptions_for_llm(),
            available_agents=[agent.name for agent in get_subagents()],
        ),
    ]
    ordered_plugins = _order_catalog_plugins(plugins)
    return tracker, ToolRegistry(ordered_plugins, capabilities=TOOL_CAPABILITIES)


def scoped_tool_registry(
    registry: ToolRegistry,
    policy: object | None,
    *,
    skills_api_provider: Callable[[str], Any] | None = None,
) -> ToolRegistry:
    scoped = registry.filtered_copy(registry.ids())
    if not isinstance(policy, ProfileToolPolicy):
        return scoped
    resources = policy.resource_policy
    if resources.hitl_mode != "autonomous":
        return scoped

    allowed_mcp_servers = frozenset(resources.mcp_servers or ())
    mcp = scoped.get("mcp")
    if not allowed_mcp_servers:
        scoped = scoped.filtered_copy(set(scoped.ids()) - {"mcp"})
    elif isinstance(mcp, McpGatewayTool):
        scoped_mcp = mcp.scoped(allowed_mcp_servers)
        scoped.replace(
            scoped_mcp.id,
            scoped_mcp,
            scoped_mcp.description,
            scoped_mcp.parameters_schema(),
        )

    allowed_skills = frozenset(resources.skills or ())
    if not allowed_skills:
        return scoped.filtered_copy(set(scoped.ids()) - {"skill"})
    if skills_api_provider is None or scoped.get("skill") is None:
        return scoped.filtered_copy(set(scoped.ids()) - {"skill"})

    skill = ReadOnlySkillsTool(
        skills_api_provider,
        allowed_names=allowed_skills,
    )
    scoped.replace(
        skill.id,
        skill,
        skill.description,
        skill.parameters_schema(),
    )
    return scoped


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
            registry.register_plugin(plugin, capability=TOOL_CAPABILITIES[plugin.id])




def bind_scoped_tools(
    registry: ToolRegistry,
    *,
    authorization: Any,
    files: Any,
    process_sandbox: Any,
    lsp_operations: Any,
    format_after_edit_enabled: bool,
) -> None:
    from voidx.tooling.adapters.lsp_post_edit import LspPostEditFormatter
    from voidx.tooling.adapters.scoped_plugin import bind_scoped_plugins

    bind_scoped_plugins(
        registry,
        authorization=authorization,
        files=files,
        process_sandbox=process_sandbox,
        formatter=(
            LspPostEditFormatter(
                lsp_operations,
                enabled=format_after_edit_enabled,
            )
            if lsp_operations is not None
            else None
        ),
    )

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
    "scoped_tool_registry",
    "register_agent_tool",
    "resolve_mcp_references",
]
