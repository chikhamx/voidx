"""Explicit factory and runtime binding for Agent-owned tool plugins."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Callable

from voidx.agent.adapters.tools.automation.goal import GoalTool
from voidx.agent.adapters.tools.automation.loop import LoopTool
from voidx.agent.adapters.tools.automation.workflow import WorkflowTool
from voidx.agent.adapters.tools.compaction import CompactContextTool
from voidx.agent.adapters.tools.context import AgentToolExecutionContext, AgentToolRuntime
from voidx.agent.adapters.tools.interaction.checkpoint import PlanCheckpointTool
from voidx.agent.adapters.tools.interaction.clarify import ClarifyTool
from voidx.agent.adapters.tools.subagent import AgentTool
from voidx.agent.adapters.tools.subagent_control import AgentControlTool
from voidx.agent.adapters.tools.todo import TodoWriteTool
from voidx.tooling.domain.context import ToolExecutionContext
from voidx.tooling.domain.result import ToolResult
from voidx.tooling.ports.tool import ToolPlugin


@dataclass
class AgentToolPlugin:
    tool: Any
    runtime: AgentToolRuntime

    @property
    def id(self) -> str:
        return self.tool.id

    @property
    def description(self) -> str:
        return self.tool.description

    def parameters_schema(self) -> dict[str, Any]:
        return self.tool.parameters_schema()

    async def execute(self, args: dict[str, Any], ctx: ToolExecutionContext) -> ToolResult:
        if isinstance(ctx, AgentToolExecutionContext):
            return await self.tool.execute(args, ctx)
        values = {
            name: getattr(ctx, name)
            for name in ToolExecutionContext.model_fields
        }
        agent_ctx = AgentToolExecutionContext(**values, runtime=self.runtime)
        return await self.tool.execute(args, agent_ctx)


def bind_agent_tool_runtime(registry: Any, runtime: AgentToolRuntime) -> None:
    if not hasattr(registry, "list") or not hasattr(registry, "get"):
        return
    for tool_def in registry.list():
        plugin = registry.get(tool_def.id)
        if isinstance(plugin, AgentToolPlugin):
            plugin.runtime = runtime


def build_agent_plugins(
    *,
    tracker: Any = None,
    subagent_runner: Callable[..., Any] | None = None,
    agent_resolver: Callable[[str], Any | None] | None = None,
    child_agent_descriptions: str = "",
    available_agents: Iterable[str] = (),
    runtime: AgentToolRuntime | None = None,
) -> list[ToolPlugin]:
    runtime = runtime or AgentToolRuntime()
    tools = [
        ClarifyTool(),
        PlanCheckpointTool(),
        WorkflowTool(),
        CompactContextTool(),
        GoalTool(),
        LoopTool(),
        TodoWriteTool(tracker=tracker),
        AgentTool(
            runner=subagent_runner,
            agent_resolver=agent_resolver,
            child_agent_descriptions=child_agent_descriptions,
            available_agents=available_agents,
        ),
        AgentControlTool(),
    ]
    return [AgentToolPlugin(tool, runtime) for tool in tools]


__all__ = ["AgentToolPlugin", "bind_agent_tool_runtime", "build_agent_plugins"]
