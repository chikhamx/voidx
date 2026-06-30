"""Dependency wiring helpers for the agent graph."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from voidx.agent.agents import child_agent_descriptions_for_llm, get_agent, get_subagents
from voidx.config import Config, Settings
from voidx.llm.compaction import CompactionService
from voidx.llm.service import get_context_limit
from voidx.llm.usage import UsageStats
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
        parallel_subagents_enabled=config.parallel_subagents.enabled,
    )
    registry.register("agent", agent_tool, agent_tool.description, agent_tool.parameters_schema())


def build_permission_service(config: Config, *, notifier: Callable[[str], object]) -> PermissionService:
    from voidx.memory.store import DATA_DIR

    extra_paths = list(config.sandbox_workspace_write)
    data_dir = str(DATA_DIR.resolve())
    if data_dir not in extra_paths:
        extra_paths.append(data_dir)
    return PermissionService(
        permission_mode=config.permission_mode.value,
        sandbox_mode=config.sandbox_mode.value,
        sandbox_workspace_write=extra_paths,
        approval_policy=config.approval_policy.value,
        approval_reviewer=config.approval_reviewer.value,
        notifier=notifier,
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
) -> tuple[Any, Any]:
    from voidx.lsp import LspManager
    from voidx.mcp import McpManager

    return (
        McpManager(
            settings=settings,
            registry=tools,
            permission=permission,
        ),
        LspManager(workspace),
    )
