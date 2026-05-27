"""Task tool — spawn sub-agents with isolated context. Depth limit = 1.

Inspired by opencode's TaskTool: creates a fresh agent instance, runs it with
only the task description (no parent history), returns summary to parent.
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

from voidx.agent.agents import get_agent, subagent_descriptions_for_llm, AgentDef
from voidx.tools.base import BaseTool, model_to_json_schema, ToolContext, ToolResult


class TaskInput(BaseModel):
    subagent_type: str = Field(
        description="Type of sub-agent to spawn: explore, plan, implement, or review"
    )
    description: str = Field(
        description="Complete, self-contained task description for the sub-agent. "
                    "Include all context the sub-agent needs — it cannot see the "
                    "conversation history."
    )
    model: str | None = Field(
        default=None,
        description="Optional model override (e.g. 'deepseek-v4-flash' for cheap tasks)"
    )


class TaskTool(BaseTool):
    id = "task"
    description = (
        "Launch a sub-agent to handle a specific task with isolated context. "
        "Use this to delegate work to specialist agents.\n\n"
        + subagent_descriptions_for_llm() +
        "\n\nIMPORTANT: Provide a complete, self-contained task description. "
        "The sub-agent cannot see the conversation history — it only sees your "
        "task description. Be specific about what files to examine/modify, what "
        "the expected output format is, and any constraints."
    )

    def __init__(self, orchestrator_func=None):
        super().__init__()
        # orchestrator_func: async callable to run a sub-agent
        self._run_subagent = orchestrator_func

    def parameters_schema(self) -> dict:
        return model_to_json_schema(TaskInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = TaskInput.model_validate(args)

        agent_def = get_agent(inp.subagent_type)
        if not agent_def:
            available = [a.name for a in [get_agent(n) for n in ["explore","plan","implement","review"]] if a]
            return ToolResult(
                output=f"Unknown agent type: {inp.subagent_type}. Available: {available}"
            )

        if agent_def.name == "orchestrator":
            return ToolResult(output="Cannot spawn orchestrator as a sub-agent.")

        if not self._run_subagent:
            return ToolResult(
                output=f"Sub-agent execution not available. Task: {inp.description[:200]}"
            )

        try:
            output = await self._run_subagent(agent_def, inp.description, inp.model)
            return ToolResult(
                title=f"{agent_def.name}: {inp.description[:60]}",
                output=output,
                metadata={"agent": agent_def.name, "model": inp.model or agent_def.model or "default"},
            )
        except Exception as e:
            return ToolResult(
                output=f"Sub-agent '{agent_def.name}' failed: {e}",
                metadata={"agent": agent_def.name, "error": str(e)},
            )
