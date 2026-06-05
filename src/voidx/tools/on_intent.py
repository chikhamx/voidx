"""Runtime intent refinement tool."""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, Field

from voidx.agent.task_state import ToolStatePatch
from voidx.agent.runtime_context import TaskIntent
from voidx.skills.runtime import SkillRunState
from voidx.tools.base import BaseTool, ToolContext, ToolResult, model_to_json_schema


class OnIntentInput(BaseModel):
    intent: TaskIntent = Field(
        description=(
            "Best refined intent for the current user request: chat, inspect, "
            "design, review, implement, debug, or ambiguous."
        )
    )
    confidence: float = Field(
        ge=0,
        le=1,
        description="Confidence in the refined intent, from 0 to 1.",
    )
    reason: str = Field(
        description="Short reason explaining why this intent fits the user request."
    )
    scope: str = Field(
        default="",
        description="Concise task scope inferred from the current user request.",
    )
    suggested_skills: list[str] = Field(
        default_factory=list,
        description="Optional skill names that may be relevant for this intent.",
    )


class OnIntentResult(BaseModel):
    confirmed_intent: TaskIntent
    confidence: float
    reason: str
    phase: str
    active_skill_runs: list[SkillRunState] = Field(default_factory=list)
    available_tool_ids: list[str] = Field(default_factory=list)
    needs_user_confirmation: bool = False
    state_patch: ToolStatePatch
    skill_instructions: list[str] = Field(default_factory=list)


IntentResolver = Callable[[OnIntentInput, ToolContext], OnIntentResult | Awaitable[OnIntentResult]]


class OnIntentTool(BaseTool):
    id = "on_intent"
    description = (
        "Refine the current task intent with runtime support. Use this before "
        "workspace/tool actions when the current intent is chat or ambiguous but "
        "the user request may require inspecting, designing, reviewing, debugging, "
        "or implementing. The runtime confirms the intent, activates workflow "
        "skills, returns available tool IDs for the refined intent, and updates "
        "structured task state. This tool does not grant permission; normal "
        "permission checks still apply to later tool calls."
    )

    def __init__(self, resolver: IntentResolver | None = None) -> None:
        super().__init__()
        self._resolver = resolver

    def parameters_schema(self) -> dict:
        schema = model_to_json_schema(OnIntentInput)
        schema["properties"]["intent"] = {
            "type": "string",
            "enum": [intent.value for intent in TaskIntent],
            "description": OnIntentInput.model_fields["intent"].description,
        }
        return schema

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = OnIntentInput.model_validate(args)
        if self._resolver is None:
            return ToolResult(
                output="Intent refinement is not available in this runtime.",
                metadata={"error": True},
            )

        result = self._resolver(inp, ctx)
        if inspect.isawaitable(result):
            result = await result
        payload = result.model_dump(mode="json")
        return ToolResult(
            title=f"intent: {result.confirmed_intent.value}",
            output=_format_on_intent_output(payload),
            metadata={
                "on_intent": payload,
                "state_patch": result.state_patch.model_dump(mode="json", exclude_unset=True),
            },
        )


def _format_on_intent_output(payload: dict) -> str:
    compact = {
        "confirmed_intent": payload.get("confirmed_intent"),
        "confidence": payload.get("confidence"),
        "reason": payload.get("reason"),
        "phase": payload.get("phase"),
        "active_skill_runs": [
            item.get("name")
            for item in payload.get("active_skill_runs", [])
            if isinstance(item, dict) and item.get("name")
        ],
        "available_tool_ids": payload.get("available_tool_ids", []),
        "needs_user_confirmation": payload.get("needs_user_confirmation", False),
    }
    parts = [json.dumps(compact, ensure_ascii=False, indent=2)]
    instructions = payload.get("skill_instructions") or []
    if instructions:
        parts.append("## Active Skill Instructions\n" + "\n\n".join(instructions))
    return "\n\n".join(parts)
