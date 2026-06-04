"""Agent tool — start an isolated child agent with filtered context."""

from __future__ import annotations

from pydantic import BaseModel, Field

from voidx.agent.agents import child_agent_descriptions_for_llm, get_agent
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
            "Include all context it needs because it receives only a filtered "
            "slice of the parent conversation."
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
        + child_agent_descriptions_for_llm()
        + "\n\nIMPORTANT: Provide a complete, self-contained task description. "
        "The child agent receives only a filtered slice of the parent context plus "
        "your task description."
    )

    def __init__(self, runner=None):
        super().__init__()
        self._run_child_agent = runner

    def parameters_schema(self) -> dict:
        return model_to_json_schema(AgentInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = AgentInput.model_validate(args)

        agent_def = get_agent(inp.agent)
        if not agent_def:
            available = [
                a.name
                for a in [get_agent(n) for n in ["explore", "plan", "implement", "review"]]
                if a
            ]
            return ToolResult(output=f"Unknown child agent: {inp.agent}. Available: {available}")

        if agent_def.name == "orchestrator":
            return ToolResult(output="Cannot run orchestrator as a child agent.")

        if not self._run_child_agent:
            return ToolResult(
                output=f"Child agent execution not available. Task: {inp.description[:200]}"
            )

        try:
            output = await self._run_child_agent(agent_def, inp.description, inp.model)
            return ToolResult(
                title=f"{agent_def.name}: {inp.description[:60]}",
                output=output,
                metadata={"agent": agent_def.name, "model": inp.model or agent_def.model or "default"},
            )
        except Exception as exc:
            return ToolResult(
                output=f"Child agent '{agent_def.name}' failed: {exc}",
                metadata={"agent": agent_def.name, "error": str(exc)},
            )
