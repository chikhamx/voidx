"""Dependency wiring helpers for the agent graph."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from voidx.agent.agents import child_agent_descriptions_for_llm, get_agent, get_subagents
from voidx.config import Config, Settings
from voidx.llm.compaction import CompactionService
from voidx.llm.provider import get_context_limit
from voidx.llm.usage import UsageStats
from voidx.permission.service import PermissionService
from voidx.tools.agent import AgentTool
from voidx.tools.on_intent import OnIntentInput, OnIntentTool
from voidx.tools.registry import ToolRegistry
from voidx.tools.task_tracker import TaskTracker


def bind_settings_to_catalog(settings: Settings | None) -> None:
    if settings is None:
        return
    from voidx.llm.catalog import bind_settings

    bind_settings(settings)


def build_tool_registry(
    *,
    settings: Settings | None,
    config: Config,
    on_intent_resolver: Callable[[OnIntentInput, Any], Any],
    subagent_runner: Callable[..., Any],
) -> tuple[TaskTracker, ToolRegistry]:
    tracker = TaskTracker()
    registry = ToolRegistry(settings=settings, tracker=tracker)

    intent_tool = OnIntentTool(resolver=on_intent_resolver)
    registry.register("on_intent", intent_tool, intent_tool.description, intent_tool.parameters_schema())

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
    return PermissionService(
        permission_mode=config.permission_mode.value,
        sandbox_mode=config.sandbox_mode.value,
        sandbox_workspace_write=config.sandbox_workspace_write,
        approval_policy=config.approval_policy.value,
        approval_reviewer=config.approval_reviewer.value,
        notifier=notifier,
    )


def build_compaction_service(config: Config) -> tuple[UsageStats, CompactionService]:
    context_limit = get_context_limit(config.model.provider)
    return (
        UsageStats(context_limit=context_limit),
        CompactionService(
            context_limit=context_limit,
            output_token_max=config.model.max_tokens,
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
