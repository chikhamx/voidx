"""Structured clarification tool."""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from voidx.runtime import GoalSpec, IntentResolution, TaskIntent, ToolStatePatch
from voidx.tools.base import (
    BaseTool,
    ToolContext,
    ToolResult,
    UserInteraction,
    UserResponse,
    model_to_json_schema,
)


class ClarifyInput(BaseModel):
    question: str = Field(description="The specific question to ask the user.")
    options: list[str] = Field(
        default_factory=list,
        description="Suggested answers. Leave empty for open-ended questions.",
    )


class ClarifyResult(BaseModel):
    question: str
    answer: str
    cancelled: bool = False
    state_patch: ToolStatePatch | None = None


class ClarifyTool(BaseTool):
    id = "clarify"
    description = (
        "Ask the user one structured clarifying question with optional choices. "
        "Use when intent, scope, or requirements are ambiguous and explicit input "
        "is needed before proceeding. Later tool calls in the same response "
        "are deferred until the answer updates runtime state."
    )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(ClarifyInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            inp = ClarifyInput.model_validate(args)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", metadata={"error": True})
        if ctx.interact is None:
            return ToolResult(
                title="clarify: unavailable",
                output=(
                    "Clarification is not available in this runtime. "
                    "Proceed only with safe assumptions and note the ambiguity."
                ),
                metadata={"clarify_cancelled": True, "blocked": True},
            )

        response = await ctx.interact(UserInteraction(
            prompt=inp.question,
            options=inp.options,
            timeout=120.0,
        ))
        if response.cancelled:
            return ToolResult(
                title="clarify: skipped",
                output=f"User skipped clarification: {inp.question}",
                metadata={"clarify_cancelled": True},
            )

        patch = _infer_state_patch(response)
        result = ClarifyResult(
            question=inp.question,
            answer=response.value,
            state_patch=patch,
        )
        payload = result.model_dump(mode="json")
        metadata = {
            "clarify_answer": response.value,
            "state_patch": patch.model_dump(mode="json", exclude_unset=True) if patch else None,
        }
        return ToolResult(
            title=f"clarify: {response.value}",
            output=json.dumps(payload, ensure_ascii=False, indent=2),
            summary=f"answer: {response.value}",
            metadata=metadata,
        )


def _infer_state_patch(response: UserResponse) -> ToolStatePatch | None:
    answer = response.value.strip()
    if not answer:
        return None

    normalized = answer.lower()
    intent_map = {
        "general": TaskIntent.GENERAL,
        "coding": TaskIntent.CODING,
        "chat": TaskIntent.GENERAL,
        "inspect": TaskIntent.CODING,
        "design": TaskIntent.CODING,
        "review": TaskIntent.CODING,
        "implement": TaskIntent.CODING,
        "debug": TaskIntent.CODING,
    }
    if normalized in intent_map:
        goal_modes = {"inspect", "design", "review", "implement", "debug"}
        return ToolStatePatch(
            intent=IntentResolution(type=intent_map[normalized]),
            goal=GoalSpec(desc=answer) if normalized in goal_modes else None,
        )

    return None
