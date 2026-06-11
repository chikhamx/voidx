"""Agent tool — start an isolated child agent."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from pydantic import BaseModel, Field

from voidx.tools.base import BaseTool, ToolContext, ToolResult, model_to_json_schema


class AgentInput(BaseModel):
    agent: str = Field(
        description=(
            "Child agent to run: explore, plan, implement, or review. "
            "Use implement only when the user explicitly asked to modify code."
        )
    )
    description: str = Field(
        description=(
            "Complete, self-contained task description for the child agent. "
            "Include all context it needs because caller conversation history "
            "is not inherited."
        )
    )
    model: str | None = Field(
        default=None,
        description="Optional model override for this child agent.",
    )


class AgentTool(BaseTool):
    id = "agent"
    description = (
        "Start an isolated child agent for a delegated task. Use this for true "
        "sub-agent work only: broad codebase exploration, implementation, review, "
        "or planning that should run in its own context.\n\n"
        "IMPORTANT: Provide a complete, self-contained task description. "
        "The child agent receives your task description, its own instructions, "
        "and runtime context, but not caller conversation history."
    )

    def __init__(
        self,
        runner=None,
        *,
        agent_resolver: Callable[[str], Any | None] | None = None,
        child_agent_descriptions: str = "",
        available_agents: Iterable[str] = (),
        parallel_subagents_enabled: bool = False,
    ):
        super().__init__()
        self._run_child_agent = runner
        self._agent_resolver = agent_resolver
        self._available_agents = list(available_agents)
        if parallel_subagents_enabled:
            self.description = (
                self.description
                + "\n\n"
                + "When independent child-agent tasks are available, issue multiple "
                "`agent` tool calls in the same response to let them run concurrently."
            )
        if child_agent_descriptions:
            self.description = (
                self.description
                + "\n\n"
                + child_agent_descriptions
            )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(AgentInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = AgentInput.model_validate(args)

        if self._agent_resolver is None:
            return ToolResult(
                output=f"Child agent execution not available. Task: {inp.description[:200]}"
            )

        agent_def = self._agent_resolver(inp.agent) if self._agent_resolver else None
        if not agent_def:
            available = self._available_agents
            return ToolResult(output=f"Unknown child agent: {inp.agent}. Available: {available}")

        agent_name = str(getattr(agent_def, "name", inp.agent))
        if agent_name == "orchestrator":
            return ToolResult(output="Cannot run orchestrator as a child agent.")

        if not self._run_child_agent:
            return ToolResult(
                output=f"Child agent execution not available. Task: {inp.description[:200]}"
            )

        try:
            output = await self._run_child_agent(agent_def, inp.description, inp.model)
            return ToolResult(
                title=f"{agent_name}: {inp.description[:60]}",
                output=output,
                metadata={
                    "agent": agent_name,
                    "model": inp.model or getattr(agent_def, "model", None) or "default",
                },
            )
        except Exception as exc:
            return ToolResult(
                output=f"Child agent '{agent_name}' failed: {exc}",
                metadata={"agent": agent_name, "error": str(exc)},
            )
