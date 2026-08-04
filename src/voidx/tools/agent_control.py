"""Control an existing child-agent run."""

from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from voidx.tools.base import BaseTool, ToolContext, ToolResult, model_to_json_schema
from voidx.ui.output.agent_display import subagent_display_name


class AgentControlInput(BaseModel):
    action: Literal["wait", "cancel"]
    run_id: str
    wait: Literal["brief", "extended", "until_complete"] = Field(
        default="until_complete",
        description="Wait strategy; ignored for cancel.",
    )


_WAIT_TIMEOUTS = {"brief": 5.0, "extended": 30.0, "until_complete": 0.0}


class AgentControlTool(BaseTool):
    id = "agent_control"
    description = "Wait for or cancel an existing child-agent run."

    def parameters_schema(self) -> dict:
        return model_to_json_schema(AgentControlInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            inp = AgentControlInput.model_validate(args)
        except ValidationError as exc:
            return ToolResult(
                output=f"Agent control rejected: {exc.errors()[0].get('msg', 'invalid arguments')}",
                metadata={"error": True, "validation_error": True},
            )
        if ctx.agent_gateway is None or not ctx.agent_run_id:
            return ToolResult(
                output="Agent gateway is unavailable for agent_control.",
                metadata={"error": True, "reason": "gateway_unavailable"},
            )
        try:
            if inp.action == "wait":
                run = await ctx.agent_gateway.wait(
                    requester_run_id=ctx.agent_run_id,
                    target_run_id=inp.run_id,
                    timeout=_WAIT_TIMEOUTS[inp.wait],
                )
            else:
                run = await ctx.agent_gateway.cancel(
                    requester_run_id=ctx.agent_run_id,
                    target_run_id=inp.run_id,
                )
        except Exception as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            return ToolResult(
                output=f"agent_control({inp.action}) failed: {detail}",
                metadata={"error": True, "reason": "gateway_error", "detail": detail[:200]},
            )
        display_name = subagent_display_name(run.run_id)
        return ToolResult(
            output=_result_output(run.result) or run.error or run.status,
            display=f"{display_name} {run.status}.",
            summary=f"{display_name} {run.status}",
            metadata={"run": run.model_dump(mode="json"), "status": run.status},
            next_step_hint=(
                f"Use agent_control to wait for {run.run_id} again."
                if inp.action == "wait" and run.status not in {"completed", "failed", "cancelled"}
                else ""
            ),
        )


def _result_output(result: dict | None) -> str:
    if not result:
        return ""
    for key in ("result", "output", "content", "text"):
        if key in result:
            return str(result.get(key) or "")
    return ""


__all__ = ["AgentControlInput", "AgentControlTool", "_WAIT_TIMEOUTS"]
