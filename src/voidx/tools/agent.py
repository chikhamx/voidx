"""Agent tool — start an isolated child agent."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from voidx.permission.service import SubagentPermissionSnapshot
from voidx.runtime.intent import TaskIntent
from voidx.runtime.task_state import (
    GoalResolution,
    GoalSpec,
    IntentResolution,
    PlanResolution,
)
from voidx.tools.base import (
    BaseTool,
    ToolContext,
    ToolResult,
    drop_nullish_tool_fields,
    keep_tool_args,
    model_to_json_schema,
    tool_timeout_metadata,
)
from voidx.workflow.dag import DEFAULT_WORKFLOW_DAG
from voidx.ui.output.agent_display import subagent_display_name
from voidx.tools.agent_control import AgentControlInput, AgentControlTool


class AgentResultContract(BaseModel):
    schema_name: str = Field(
        default="agent_result",
        description="Name of the structured result contract the child agent must return.",
    )
    format: str = Field(description="Concrete structured result fields and allowed values.")


class AgentInput(BaseModel):
    mode: Literal["review", "debug", "implement"] = Field(
        description="Kind of bounded child-agent work."
    )
    goal: str = Field(description="One-sentence outcome to achieve.")
    detail: str = Field(description="Complete execution brief, constraints, and acceptance criteria.")
    scope: str | None = Field(
        default=None,
        description="Optional file, module, directory, behavior, or issue scope.",
    )


@dataclass(frozen=True)
class NormalizedAgentDelegation:
    description: str
    goal_resolution: GoalResolution
    result_contract: AgentResultContract


_MODE_ROUTES: dict[str, tuple[str, str, str]] = {
    "review": ("review", "review", "review"),
    "debug": ("debug", "debug", "debug"),
    "implement": ("feature", "tdd", "verify"),
}

_RESULT_PRESETS: dict[str, AgentResultContract] = {
    "review": AgentResultContract(
        schema_name="review_result",
        format="verdict=PASS|FAIL|NEEDS_CHANGE, findings, risks, next_actions",
    ),
    "debug": AgentResultContract(
        schema_name="debug_result",
        format="root_cause, evidence, reproduction, fix_direction, open_questions",
    ),
    "implement": AgentResultContract(
        schema_name="implementation_result",
        format="status, files_changed, tests_run, risks, followups",
    ),
}


def _normalize_agent_args(args):
    if not isinstance(args, dict):
        return args
    if args.get("action") in {"wait", "cancel"}:
        return args
    if "goal" not in args and "task" in args:
        args = {
            **args,
            "goal": args.get("task", ""),
            "detail": args.get("success_criteria") or args.get("task", ""),
            "scope": args.get("target"),
        }
    if args.get("mode") == "inspect":
        args = {**args, "mode": "review"}
    if args.get("mode") == "feedback":
        args = {**args, "mode": "implement"}
    return keep_tool_args(args, {"mode", "goal", "detail", "scope"})


class AgentTool(BaseTool):
    id = "agent"
    description = (
        "Start one isolated child agent for an independent task and return its run_id. "
        "The child does not inherit the caller's conversation history."
    )

    def __init__(
        self,
        runner=None,
        *,
        agent_resolver: Callable[[str], Any | None] | None = None,
        child_agent_descriptions: str = "",
        available_agents: Iterable[str] = (),
    ):
        super().__init__()
        self._run_child_agent = runner
        self._agent_resolver = agent_resolver
        self._available_agents = list(available_agents)
        if child_agent_descriptions:
            self.description = (
                self.description
                + "\n\n"
                + child_agent_descriptions
            )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(AgentInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        args = _normalize_agent_args(args)
        if isinstance(args, dict) and args.get("action") in {"wait", "cancel"}:
            control_args = {
                "action": args["action"],
                "run_id": args.get("target_run_id", ""),
                "wait": (
                    "until_complete"
                    if not args.get("timeout")
                    else "brief"
                    if float(args["timeout"]) <= 5
                    else "extended"
                ),
            }
            return await AgentControlTool().execute(control_args, ctx)
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
                output=f"Child agent delegation rejected.{detail}",
                metadata={"error": True, "validation_error": True},
            )
        rejection = _delegation_rejection(inp)
        if rejection:
            return ToolResult(output=rejection, metadata={"error": True, "delegation_rejected": True})
        normalized = normalize_agent_input(inp)

        if self._agent_resolver is None:
            return ToolResult(
                output=f"Child agent execution not available. Task: {normalized.description[:200]}",
                metadata={"error": True, "reason": "no_resolver"},
            )

        agent_def = self._agent_resolver("voidx") if self._agent_resolver else None
        if not agent_def:
            return ToolResult(output="Unknown child agent: voidx.", metadata={"error": True, "reason": "unknown_agent"})

        agent_def_name = str(getattr(agent_def, "name", "voidx"))

        if not self._run_child_agent:
            return ToolResult(
                output=f"Child agent execution not available. Task: {normalized.description[:200]}",
                metadata={"error": True, "reason": "no_runner"},
            )

        try:
            if ctx.agent_gateway is None or not ctx.agent_run_id:
                return ToolResult(
                    output="Agent gateway is required for agent(spawn).",
                    metadata={"agent": agent_def_name, "error": True, "reason": "gateway_unavailable"},
                )

            async def gateway_runner(agent_run_id: str) -> str | dict[str, Any]:
                run_metadata: dict[str, object] = {}
                result = await self._run_child_agent(
                    agent_def,
                    normalized.description,
                    normalized.goal_resolution,
                    normalized.result_contract,
                    **_runner_kwargs(
                        self._run_child_agent,
                        ctx,
                        agent_run_id=agent_run_id,
                        run_metadata=run_metadata,
                    ),
                )
                finish_reason = str(run_metadata.get("finish_reason") or "")
                if finish_reason and finish_reason not in {"final_answer", "message_result"}:
                    return {"result": result, "finish_reason": finish_reason}
                return result

            run = await ctx.agent_gateway.spawn(
                session_id=ctx.session_id,
                parent_run_id=ctx.agent_run_id,
                agent_name=agent_def_name,
                description=normalized.description,
                runner=gateway_runner,
            )
            goal = normalized.goal_resolution.goal
            plan = normalized.goal_resolution.plan
            metadata = {
                "agent": agent_def_name,
                "intent": normalized.goal_resolution.intent.model_dump(mode="json"),
                "goal": goal.model_dump(mode="json") if goal is not None else None,
                "workflow_route": plan.model_dump(mode="json") if plan is not None else None,
                "result_schema": normalized.result_contract.schema_name,
                "run_id": run.run_id,
                "status": run.status,
                "run": run.model_dump(mode="json"),
            }
            return ToolResult(
                title=f"{agent_def_name}: {normalized.description[:60]}",
                output=f"Child agent '{agent_def_name}' spawned with run_id {run.run_id}.",
                summary=f"{agent_def_name} spawned",
                metadata=metadata,
                next_step_hint=(
                    f"Use agent_control with run_id={run.run_id} to collect the result, "
                    "or cancel the run."
                ),
            )
        except TimeoutError as exc:
            return ToolResult(
                output=f"Child agent '{agent_def_name}' timed out: {exc}",
                metadata=tool_timeout_metadata(
                    "agent",
                    agent=agent_def_name,
                    reason="timeout",
                    detail=str(exc)[:200],
                ),
            )
        except Exception as exc:
            return ToolResult(
                output=f"Child agent '{agent_def_name}' failed: {exc}",
                metadata={"agent": agent_def_name, "error": True, "reason": "exception", "detail": str(exc)[:200]},
            )


def _result_output(result: dict[str, Any] | None) -> str:
    if not result:
        return ""
    for key in ("result", "output", "content", "text"):
        if key in result:
            return str(result.get(key) or "")
    return json.dumps(result, ensure_ascii=False, default=str)


def _runner_kwargs(
    runner,
    ctx: ToolContext,
    *,
    agent_run_id: str | None = None,
    run_metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    kwargs = _runner_permission_kwargs(runner, ctx)
    try:
        params = inspect.signature(runner).parameters
    except (TypeError, ValueError):
        return kwargs
    if agent_run_id is not None and "agent_run_id" in params:
        kwargs["agent_run_id"] = agent_run_id
    if ctx.agent_gateway is not None and "agent_gateway" in params:
        kwargs["agent_gateway"] = ctx.agent_gateway
    if run_metadata is not None and "run_metadata" in params:
        kwargs["run_metadata"] = run_metadata
    return kwargs

def _runner_permission_kwargs(runner, ctx: ToolContext) -> dict[str, object]:
    if ctx.get_access_grants is None or ctx.get_revocation_epoch is None:
        return {}
    try:
        params = inspect.signature(runner).parameters
    except (TypeError, ValueError):
        return {}
    if "permission_snapshot" not in params:
        return {}
    return {
        "permission_snapshot": SubagentPermissionSnapshot.from_parts(
            ctx.get_access_grants(),
            ctx.get_revocation_epoch(),
            current_revocation_epoch=ctx.get_revocation_epoch,
        )
    }


def normalize_agent_input(inp: AgentInput) -> NormalizedAgentDelegation:
    _goal_type, join, leave = _MODE_ROUTES[inp.mode]
    return NormalizedAgentDelegation(
        description=_description_for_child(inp),
        goal_resolution=GoalResolution(
            intent=IntentResolution(type=TaskIntent.CODING),
            goal=GoalSpec(desc=inp.goal.strip()),
            plan=PlanResolution(join=join, leave=leave),
        ),
        result_contract=_RESULT_PRESETS[inp.mode],
    )


def _description_for_child(inp: AgentInput) -> str:
    lines = [f"Goal: {inp.goal.strip()}"]
    if inp.scope and inp.scope.strip():
        lines.append(f"Scope: {inp.scope.strip()}")
    lines.extend(["", "Details:", inp.detail.strip()])
    return "\n".join(lines)


def _delegation_rejection(inp: AgentInput) -> str:
    if not inp.goal.strip():
        return "Child agent delegation rejected. goal must not be empty."
    if len("".join(inp.detail.split())) < 12:
        return "Child agent delegation rejected. detail must be a complete execution brief."
    _goal_type, join, leave = _MODE_ROUTES[inp.mode]
    if join not in DEFAULT_WORKFLOW_DAG.nodes or leave not in DEFAULT_WORKFLOW_DAG.nodes:
        return "Child agent delegation rejected. Internal mode routing failed."
    return ""
