"""Agent tool — start an isolated child agent."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from voidx.runtime.intent import TaskIntent
from voidx.runtime.task_state import (
    GoalResolution,
    GoalSpec,
    GoalType,
    IntentResolution,
    PlanResolution,
)
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
        description="Child agent identity to run. Use voidx.",
    )
    mode: Literal["inspect", "review", "debug", "plan", "implement", "feedback"] = Field(
        description="Kind of bounded child-agent work. Drives internal workflow routing.",
    )
    task: str = Field(
        description=(
            "Complete, self-contained task brief for the child agent. "
            "Caller conversation history is not inherited."
        ),
    )
    target: str = Field(
        description="Single file, module, directory, behavior, or issue scope for this child task.",
    )
    success_criteria: str = Field(
        default="",
        description="What counts as done. Required for implement and feedback modes.",
    )
    result_preset: Literal[
        "auto",
        "inspection",
        "review",
        "debug",
        "plan",
        "implementation",
        "feedback",
    ] = Field(
        default="auto",
        description="Short enum selecting the internal structured child result contract.",
    )
    model: str | None = Field(default=None, description="Optional model override for this child agent.")


@dataclass(frozen=True)
class NormalizedAgentDelegation:
    description: str
    goal_resolution: GoalResolution
    result_contract: AgentResultContract
    model: str | None


_MODE_ROUTES: dict[str, tuple[GoalType, str, str]] = {
    "inspect": (GoalType.INSPECT, "review", "review"),
    "review": (GoalType.REVIEW, "review", "review"),
    "debug": (GoalType.DEBUG, "debug", "debug"),
    "plan": (GoalType.DESIGN, "plan", "plan"),
    "implement": (GoalType.FEATURE, "tdd", "verify"),
    "feedback": (GoalType.REVIEW, "feedback", "verify"),
}

_AUTO_PRESETS: dict[str, str] = {
    "inspect": "inspection",
    "review": "review",
    "debug": "debug",
    "plan": "plan",
    "implement": "implementation",
    "feedback": "feedback",
}

_ALLOWED_PRESETS: dict[str, set[str]] = {
    "inspect": {"inspection", "review"},
    "review": {"review"},
    "debug": {"debug", "inspection"},
    "plan": {"plan", "inspection"},
    "implement": {"implementation"},
    "feedback": {"feedback", "implementation"},
}

_RESULT_PRESETS: dict[str, AgentResultContract] = {
    "inspection": AgentResultContract(
        schema_name="inspection_result",
        format="summary, evidence, findings, open_questions",
    ),
    "review": AgentResultContract(
        schema_name="review_result",
        format="verdict=PASS|FAIL|NEEDS_CHANGE, findings, risks, next_actions",
    ),
    "debug": AgentResultContract(
        schema_name="debug_result",
        format="root_cause, evidence, reproduction, fix_direction, open_questions",
    ),
    "plan": AgentResultContract(
        schema_name="plan_result",
        format="plan_summary, tasks, files, tests, risks",
    ),
    "implementation": AgentResultContract(
        schema_name="implementation_result",
        format="status, files_changed, tests_run, risks, followups",
    ),
    "feedback": AgentResultContract(
        schema_name="feedback_result",
        format="feedback_status, accepted, rejected, changes_needed, verification_notes",
    ),
}


class AgentTool(BaseTool):
    id = "agent"
    description = (
        "Start an isolated child agent for a delegated task. Use ONLY when "
        "you need to run multiple independent tasks in parallel, or the user "
        "explicitly asks for a child agent. Do not use for single-file reads, "
        "simple searches, or straightforward tasks you can do directly. Each "
        "call must include mode, task, and one concrete target. Use "
        "success_criteria for implement and feedback modes, and result_preset "
        "when the child output shape should be explicit. "
        "The child agent receives your task description and runtime context, "
        "but not caller conversation history."
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
                    f"{detail} The main agent must provide mode, task, and target "
                    "for each delegated task."
                ),
                metadata={"error": True, "validation_error": True},
            )
        requested_agent = inp.agent

        rejection = _delegation_rejection(inp)
        if rejection:
            return ToolResult(output=rejection, metadata={"error": True, "delegation_rejected": True})
        normalized = normalize_agent_input(inp)

        if self._agent_resolver is None:
            return ToolResult(
                output=f"Child agent execution not available. Task: {normalized.description[:200]}"
            )

        agent_def = self._agent_resolver(requested_agent) if self._agent_resolver else None
        if not agent_def:
            available = self._available_agents
            return ToolResult(output=f"Unknown child agent: {inp.agent}. Available: {available}")

        agent_def_name = str(getattr(agent_def, "name", inp.agent))

        if not self._run_child_agent:
            return ToolResult(
                output=f"Child agent execution not available. Task: {normalized.description[:200]}"
            )

        try:
            output = await self._run_child_agent(
                agent_def,
                normalized.description,
                normalized.model,
                normalized.goal_resolution,
                normalized.result_contract,
            )
            goal = normalized.goal_resolution.goal
            plan = normalized.goal_resolution.plan
            return ToolResult(
                title=f"{agent_def_name}: {normalized.description[:60]}",
                output=output,
                summary=f"{agent_def_name} completed",
                metadata={
                    "agent": agent_def_name,
                    "intent": normalized.goal_resolution.intent.model_dump(mode="json"),
                    "goal": goal.model_dump(mode="json") if goal is not None else None,
                    "workflow_route": plan.model_dump(mode="json") if plan is not None else None,
                    "result_schema": normalized.result_contract.schema_name,
                    "model": normalized.model or getattr(agent_def, "model", None) or "default",
                },
            )
        except Exception as exc:
            return ToolResult(
                output=f"Child agent '{agent_def_name}' failed: {exc}",
                metadata={"agent": agent_def_name, "error": str(exc)},
            )


def normalize_agent_input(inp: AgentInput) -> NormalizedAgentDelegation:
    goal_type, join, leave = _MODE_ROUTES[inp.mode]
    preset = _AUTO_PRESETS[inp.mode] if inp.result_preset == "auto" else inp.result_preset
    result_contract = _RESULT_PRESETS[preset]
    description = _description_for_child(inp)
    goal_resolution = GoalResolution(
        intent=IntentResolution(type=TaskIntent.CODING, desc=inp.task.strip()),
        goal=GoalSpec(type=goal_type, desc=f"{inp.mode}: {inp.target.strip()}"),
        plan=PlanResolution(join=join, leave=leave),
    )
    return NormalizedAgentDelegation(
        description=description,
        goal_resolution=goal_resolution,
        result_contract=result_contract,
        model=inp.model,
    )


def _description_for_child(inp: AgentInput) -> str:
    success_criteria = inp.success_criteria.strip() or "Report concrete findings and blockers."
    return "\n".join(
        [
            f"Task: {inp.task.strip()}",
            f"Mode: {inp.mode}",
            f"Target: {inp.target.strip()}",
            f"Success criteria: {success_criteria}",
        ]
    )


def _delegation_rejection(inp: AgentInput) -> str:
    if len("".join(inp.task.split())) < 12:
        return "Child agent delegation rejected. task must be a complete, self-contained brief."
    if not inp.target.strip():
        return f"Child agent delegation rejected. mode='{inp.mode}' requires target."
    if inp.mode in {"implement", "feedback"} and not inp.success_criteria.strip():
        return f"Child agent delegation rejected. mode='{inp.mode}' requires success_criteria."
    preset = _AUTO_PRESETS[inp.mode] if inp.result_preset == "auto" else inp.result_preset
    allowed = _ALLOWED_PRESETS[inp.mode]
    if preset not in allowed:
        allowed_text = ", ".join(sorted(allowed))
        return (
            "Child agent delegation rejected. "
            f"result_preset='{preset}' is not valid for mode='{inp.mode}'. "
            f"Allowed: {allowed_text}, auto."
        )
    if preset not in _RESULT_PRESETS:
        return "Child agent delegation rejected. Internal result_preset routing failed."
    route = _MODE_ROUTES.get(inp.mode)
    if route is None:
        return "Child agent delegation rejected. Internal mode routing failed."
    _goal_type, join, leave = route
    if join not in DEFAULT_WORKFLOW_DAG.nodes:
        return "Child agent delegation rejected. mode routing produced an unknown plan.join workflow node."
    if leave not in DEFAULT_WORKFLOW_DAG.nodes:
        return "Child agent delegation rejected. mode routing produced an unknown plan.leave workflow node."
    if not _RESULT_PRESETS[preset].format.strip():
        return "Child agent delegation rejected. Internal result_preset routing failed."
    return ""
