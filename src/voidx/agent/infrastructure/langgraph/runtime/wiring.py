"""Dependency wiring helpers for the agent graph."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from voidx.agent.application.agents import child_agent_descriptions_for_llm, get_agent, get_subagents
from voidx.config import Config, Settings
from voidx.llm.compaction import CompactionService
from voidx.llm.service import create_resolver_model, get_context_limit
from voidx.llm.usage import UsageStats
from voidx.permission.grants import persistent_grants_from_paths
from voidx.permission.service import PermissionService
from voidx.tools.service import AgentTool, ToolRegistry, TaskTracker


def bind_settings_to_catalog(settings: Settings | None) -> None:
    if settings is None:
        return
    from voidx.llm.catalog import bind_settings

    bind_settings(settings)


def build_tool_registry(
    *,
    settings: Settings | None,
    config: Config,
    subagent_runner: Callable[..., Any],
) -> tuple[TaskTracker, ToolRegistry]:
    tracker = TaskTracker()
    registry = ToolRegistry(settings=settings, tracker=tracker)

    register_agent_tool(
        registry,
        config=config,
        subagent_runner=subagent_runner,
    )

    return tracker, registry


def register_agent_tool(
    registry: ToolRegistry,
    *,
    config: Config,
    subagent_runner: Callable[..., Any],
) -> None:
    agent_tool = AgentTool(
        runner=subagent_runner,
        agent_resolver=get_agent,
        child_agent_descriptions=child_agent_descriptions_for_llm(),
        available_agents=[agent.name for agent in get_subagents()],
    )
    registry.register("agent", agent_tool, agent_tool.description, agent_tool.parameters_schema())


def build_permission_service(
    config: Config,
    *,
    settings: Settings | None = None,
    notifier: Callable[[str], object],
) -> PermissionService:
    from voidx.memory.store import DATA_DIR

    writable_dirs = list(config.sandbox_writable_dirs)
    data_dir = str(DATA_DIR.resolve())
    if data_dir not in writable_dirs:
        writable_dirs.append(data_dir)
    return PermissionService(
        permission_mode=config.permission_mode.value,
        sandbox_readable_files=list(config.sandbox_readable_files),
        sandbox_readable_dirs=list(config.sandbox_readable_dirs),
        sandbox_writable_files=list(config.sandbox_writable_files),
        sandbox_writable_dirs=writable_dirs,
        persistent_grants=persistent_grants_from_paths(
            settings.get_persistent_readable_files(),
            settings.get_persistent_readable_dirs(),
            settings.get_persistent_writable_files(),
            settings.get_persistent_writable_dirs(),
        ) if settings is not None else [],
        notifier=notifier,
        persistent_grant_writer=settings.add_persistent_grant_delta if settings is not None else None,
    )


def build_compaction_service(config: Config) -> tuple[UsageStats, CompactionService]:
    context_limit = get_context_limit(config.model.provider, config.model.protocol or "", config.model.context_window)
    return (
        UsageStats(context_limit=context_limit),
        CompactionService(
            context_limit=context_limit,
            output_token_max=config.model.max_tokens,
            soft_ratio=config.compaction_soft_ratio,
            post_target_ratio=config.compaction_post_target_ratio,
        ),
    )


def build_external_managers(
    *,
    settings: Settings | None,
    tools: ToolRegistry,
    permission: PermissionService,
    workspace: str,
    model: Any | None = None,
    model_config: Any | None = None,
) -> tuple[Any, Any]:
    from voidx.lsp import LspManager
    from voidx.mcp import McpManager
    from voidx.mcp.description_generator import McpDescriptionGenerator
    from voidx.mcp.gateway import McpGatewayTool

    description_model = (
        create_resolver_model(model, model_config)
        if model is not None and model_config is not None
        else model
    )
    description_generator = McpDescriptionGenerator(description_model)
    mcp_manager = McpManager(
        settings=settings,
        registry=tools,
        permission=permission,
        description_generator=description_generator.generate,
        workspace=workspace,
    )
    gateway_tool = McpGatewayTool(mcp_manager)
    tools.register(gateway_tool.id, gateway_tool, gateway_tool.description, gateway_tool.parameters_schema())

    return (
        mcp_manager,
        LspManager(workspace),
    )
