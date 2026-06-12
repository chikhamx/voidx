"""Agent tool — start an isolated child agent."""

from __future__ import annotations

from collections.abc import Callable, Iterable
import inspect
from typing import Any

from pydantic import BaseModel, Field

from voidx.tools.base import BaseTool, ToolContext, ToolResult, model_to_json_schema

_RUNTIME_PERSONAS = {"explore", "plan", "implement", "review"}


class AgentInput(BaseModel):
    agent: str = Field(
        default="sub-voidx",
        description=(
            "Child agent identity to run. Use sub-voidx."
        )
    )
    persona: str = Field(
        default="explore",
        description=(
            "Runtime persona for the child agent: explore, plan, implement, or review. "
            "Use implement only when the user explicitly asked to modify code."
        ),
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
        "Start an isolated child agent for a delegated task. Use this ONLY when "
        "you need to run multiple independent tasks in parallel, or the user "
        "explicitly asks for a child agent. Do not use for single-file reads, "
        "simple searches, or straightforward tasks you can do directly.\n\n"
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
        requested_agent = inp.agent
        runtime_persona = inp.persona
        if requested_agent in _RUNTIME_PERSONAS:
            runtime_persona = requested_agent
            requested_agent = "sub-voidx"

        if self._agent_resolver is None:
            return ToolResult(
                output=f"Child agent execution not available. Task: {inp.description[:200]}"
            )

        agent_def = self._agent_resolver(requested_agent) if self._agent_resolver else None
        if not agent_def:
            available = self._available_agents
            return ToolResult(output=f"Unknown child agent: {inp.agent}. Available: {available}")

        agent_name = str(getattr(agent_def, "name", inp.agent))
        if agent_name == "voidx":
            return ToolResult(output="Cannot run voidx as a child agent.")

        if not self._run_child_agent:
            return ToolResult(
                output=f"Child agent execution not available. Task: {inp.description[:200]}"
            )

        try:
            if _runner_accepts_persona(self._run_child_agent):
                output = await self._run_child_agent(agent_def, inp.description, inp.model, runtime_persona)
            else:
                output = await self._run_child_agent(agent_def, inp.description, inp.model)
            return ToolResult(
                title=f"{agent_name}/{runtime_persona}: {inp.description[:60]}",
                output=output,
                metadata={
                    "agent": agent_name,
                    "persona": runtime_persona,
                    "model": inp.model or getattr(agent_def, "model", None) or "default",
                },
            )
        except Exception as exc:
            return ToolResult(
                output=f"Child agent '{agent_name}' failed: {exc}",
                metadata={"agent": agent_name, "error": str(exc)},
            )


def _runner_accepts_persona(runner) -> bool:
    try:
        signature = inspect.signature(runner)
    except (TypeError, ValueError):
        return True
    params = signature.parameters
    if any(param.kind == inspect.Parameter.VAR_POSITIONAL for param in params.values()):
        return True
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values()):
        return True
    return len(params) >= 4
