"""Agent tool — start an isolated child agent."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from voidx.runtime.task_state import GoalResolution
from voidx.tools.base import BaseTool, ToolContext, ToolResult, model_to_json_schema
from voidx.workflow.dag import DEFAULT_WORKFLOW_DAG


class AgentResultContract(BaseModel):
    schema_name: str = Field(
        default="agent_result",
        description="Name of the structured result contract the child agent must return.",
    )
    format: str = Field(description="Concrete structured result fields and allowed values.")


class AgentInput(BaseModel):
    agent: str = Field(
        default="voidx",
        description=(
            "Child agent identity to run. Use voidx."
        )
    )
    description: str = Field(
        description=(
            "Complete, self-contained task description for the child agent. "
            "Include all context it needs because caller conversation history "
            "is not inherited."
        )
    )
    goal_resolution: GoalResolution = Field(
        description=(
            "Child task intent, required goal, and workflow route. "
            "plan.join and plan.leave are required and must name workflow nodes."
        )
    )
    result: AgentResultContract = Field(description="Structured result the child agent must return.")
    model: str | None = Field(default=None, description="Optional model override for this child agent.")


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
        self._parallel_subagents_enabled = parallel_subagents_enabled
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
        try:
            inp = AgentInput.model_validate(args)
        except ValidationError as exc:
            missing = [
                ".".join(str(part) for part in error.get("loc", ()))
                for error in exc.errors()
                if error.get("type") == "missing"
            ]
            detail = f" Missing required argument: {', '.join(missing)}." if missing else ""
            return ToolResult(
                output=(
                    "Child agent delegation rejected."
                    f"{detail} The main agent must provide description, goal_resolution, "
                    "and result for each delegated task."
                ),
                metadata={"error": True, "validation_error": True},
            )
        requested_agent = inp.agent

        if self._agent_resolver is None:
            return ToolResult(
                output=f"Child agent execution not available. Task: {inp.description[:200]}"
            )

        agent_def = self._agent_resolver(requested_agent) if self._agent_resolver else None
        if not agent_def:
            available = self._available_agents
            return ToolResult(output=f"Unknown child agent: {inp.agent}. Available: {available}")

        agent_def_name = str(getattr(agent_def, "name", inp.agent))

        rejection = _delegation_rejection(inp, ctx, parallel_enabled=self._parallel_subagents_enabled)
        if rejection:
            return ToolResult(output=rejection, metadata={"error": True, "delegation_rejected": True})

        if not self._run_child_agent:
            return ToolResult(
                output=f"Child agent execution not available. Task: {inp.description[:200]}"
            )

        try:
            output = await self._run_child_agent(
                agent_def,
                inp.description,
                inp.model,
                inp.goal_resolution,
                inp.result,
            )
            goal = inp.goal_resolution.goal
            plan = inp.goal_resolution.plan
            return ToolResult(
                title=f"{agent_def_name}: {inp.description[:60]}",
                output=output,
                summary=f"{agent_def_name} completed",
                metadata={
                    "agent": agent_def_name,
                    "intent": inp.goal_resolution.intent.model_dump(mode="json"),
                    "goal": goal.model_dump(mode="json") if goal is not None else None,
                    "workflow_route": plan.model_dump(mode="json") if plan is not None else None,
                    "result_schema": inp.result.schema_name,
                    "model": inp.model or getattr(agent_def, "model", None) or "default",
                },
            )
        except Exception as exc:
            return ToolResult(
                output=f"Child agent '{agent_def_name}' failed: {exc}",
                metadata={"agent": agent_def_name, "error": str(exc)},
            )


def _delegation_rejection(inp: AgentInput, ctx: ToolContext, *, parallel_enabled: bool) -> str:
    del ctx, parallel_enabled
    if len(inp.description.strip()) < 12:
        return "Child agent delegation rejected. Description must be a complete, self-contained brief."
    goal = inp.goal_resolution.goal
    if goal is None:
        return "Child agent delegation rejected. goal_resolution.goal is required."
    plan = inp.goal_resolution.plan
    if plan is None:
        return "Child agent delegation rejected. Provide goal_resolution.plan.join and plan.leave."
    join = plan.join.strip().lower()
    leave = (plan.leave or "").strip().lower()
    if not join and not leave:
        return "Child agent delegation rejected. Provide goal_resolution.plan.join and plan.leave."
    if not join:
        return "Child agent delegation rejected. goal_resolution.plan.join is required."
    if not leave:
        return "Child agent delegation rejected. goal_resolution.plan.leave is required."
    if join not in DEFAULT_WORKFLOW_DAG.nodes:
        return "Child agent delegation rejected. plan.join must be a known workflow node."
    if leave not in DEFAULT_WORKFLOW_DAG.nodes:
        return "Child agent delegation rejected. plan.leave must be a known workflow node."
    if goal.type.value == "review" and join != "review":
        return "Child agent delegation rejected. review goals must enter plan.join=review."
    if not inp.result.format.strip():
        return "Child agent delegation rejected. result.format is required."
    return ""
