"""Agent tool — start an isolated child agent."""

from __future__ import annotations

from collections.abc import Callable, Iterable
import inspect
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from voidx.tools.base import BaseTool, ToolContext, ToolResult, model_to_json_schema

class AgentInput(BaseModel):
    agent: str = Field(
        default="voidx",
        description=(
            "Child agent identity to run. Use voidx."
        )
    )
    persona: str = Field(
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
    max_steps: int = Field(
        ge=3,
        description="Step budget chosen by voidx for this delegated task.",
    )
    delegation_reason: Literal[
        "user_requested",
        "parallel_independent",
        "isolated_review",
        "context_isolation",
    ] = Field(description="Why this task needs a child agent instead of direct handling.")
    expected_output: str = Field(description="Structured result the child agent must return.")
    parent_evidence: str = Field(description="Facts the parent agent already gathered before delegating.")


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
                    f"{detail} The main agent must provide persona, description, max_steps, "
                    "delegation_reason, expected_output, and parent_evidence for each delegated task."
                ),
                metadata={"error": True, "validation_error": True},
            )
        requested_agent = inp.agent
        runtime_persona = inp.persona

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
            if _runner_accepts_persona(self._run_child_agent):
                if _runner_accepts_max_steps(self._run_child_agent):
                    output = await self._run_child_agent(
                        agent_def, inp.description, inp.model, runtime_persona,
                        max_steps=inp.max_steps,
                    )
                else:
                    output = await self._run_child_agent(agent_def, inp.description, inp.model, runtime_persona)
            else:
                output = await self._run_child_agent(agent_def, inp.description, inp.model)
            return ToolResult(
                title=f"{agent_def_name}/{runtime_persona}: {inp.description[:60]}",
                output=output,
                metadata={
                    "agent": agent_def_name,
                    "persona": runtime_persona,
                    "max_steps": inp.max_steps,
                    "delegation_reason": inp.delegation_reason,
                    "model": inp.model or getattr(agent_def, "model", None) or "default",
                },
            )
        except Exception as exc:
            return ToolResult(
                output=f"Child agent '{agent_def_name}' failed: {exc}",
                metadata={"agent": agent_def_name, "error": str(exc)},
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


def _runner_accepts_max_steps(runner) -> bool:
    try:
        signature = inspect.signature(runner)
    except (TypeError, ValueError):
        return True
    params = signature.parameters
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values()):
        return True
    return "max_steps" in params


def _delegation_rejection(inp: AgentInput, ctx: ToolContext, *, parallel_enabled: bool) -> str:
    if len(inp.description.strip()) < 12:
        return "Child agent delegation rejected. Description must be a complete, self-contained brief."
    if not inp.expected_output.strip():
        return "Child agent delegation rejected. expected_output is required."
    if not inp.parent_evidence.strip():
        return "Child agent delegation rejected. parent_evidence is required."
    if inp.delegation_reason == "parallel_independent" and not parallel_enabled:
        return "Child agent delegation rejected. parallel_independent requires parallel subagents to be enabled."
    if inp.delegation_reason == "isolated_review" and inp.persona != "review":
        return "Child agent delegation rejected. isolated_review requires persona='review'."
    if inp.persona == "review" or inp.delegation_reason == "isolated_review":
        review_rejection = _review_delegation_rejection(inp)
        if review_rejection:
            return review_rejection
    if inp.persona == "implement" and ctx.goal_type not in {"feature", "bugfix", "refactor"}:
        return "Child agent delegation rejected. implement persona requires a feature, bugfix, or refactor goal."
    return ""


def _review_delegation_rejection(inp: AgentInput) -> str:
    expected = inp.expected_output.lower()
    if not (
        "verdict" in expected
        and "pass" in expected
        and "fail" in expected
        and "needs_change" in expected
    ):
        return (
            "Child agent delegation rejected. Review expected_output must request "
            "verdict: PASS | FAIL | NEEDS_CHANGE."
        )

    evidence = inp.parent_evidence.lower()
    has_target = any(
        marker in evidence
        for marker in (
            "changed files",
            "files changed",
            "review target",
            "target:",
            "file:",
            "files:",
        )
    )
    if not has_target:
        return (
            "Child agent delegation rejected. Review parent_evidence must include "
            "changed files or review target."
        )

    has_verification = any(
        marker in evidence
        for marker in (
            "verification",
            "verified",
            "tests:",
            "test:",
            "pytest",
            "not verified",
            "not run",
            "未验证",
            "未运行",
        )
    )
    if not has_verification:
        return (
            "Child agent delegation rejected. Review parent_evidence must include "
            "verification commands or an explicit not-verified reason."
        )
    return ""
