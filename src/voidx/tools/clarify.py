"""Structured clarification tool."""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from voidx.runtime import TaskIntent, ToolStatePatch
from voidx.tools.base import (
    BaseTool,
    ToolContext,
    ToolResult,
    UserInteraction,
    UserResponse,
    model_to_json_schema,
)


class ClarifyOption(BaseModel):
    label: str = Field(description="Short display label.")
    value: str = Field(description="Machine-readable answer value.")
    description: str = Field(default="", description="One-line explanation of this option.")


class ClarifyInput(BaseModel):
    question: str = Field(description="The specific question to ask the user.")
    options: list[ClarifyOption] = Field(
        default_factory=list,
        description="Suggested answers. Leave empty for open-ended questions.",
    )
    context: str = Field(
        default="",
        description="Why this question matters or what decision depends on the answer.",
    )
    blocking: bool = Field(default=True, description="Whether the agent should wait for the answer.")


class ClarifyResult(BaseModel):
    question: str
    answer: str
    selected_option: str | None = None
    cancelled: bool = False
    state_patch: ToolStatePatch | None = None


class ClarifyTool(BaseTool):
    id = "clarify"
    description = (
        "Ask the user one structured clarifying question with optional choices. "
        "Use when intent, scope, or requirements are ambiguous and explicit input "
        "is needed before proceeding. This is a barrier tool: later tool calls in "
        "the same response are deferred until the answer updates runtime state."
    )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(ClarifyInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = ClarifyInput.model_validate(args)
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
            prompt=_prompt(inp),
            options=[(opt.label, opt.value, opt.description) for opt in inp.options],
            blocking=inp.blocking,
            timeout=120.0,
        ))
        if response.cancelled:
            return ToolResult(
                title="clarify: skipped",
                output=f"User skipped clarification: {inp.question}",
                metadata={"clarify_cancelled": True},
            )

        patch = _infer_state_patch(inp, response)
        selected_option = _selected_option(inp, response)
        result = ClarifyResult(
            question=inp.question,
            answer=response.value,
            selected_option=selected_option,
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
            metadata=metadata,
        )


def _prompt(inp: ClarifyInput) -> str:
    if not inp.context:
        return inp.question
    return f"{inp.question}\n\n{inp.context}"


def _infer_state_patch(inp: ClarifyInput, response: UserResponse) -> ToolStatePatch | None:
    answer = response.value.strip()
    if not answer:
        return None

    normalized = answer.lower()
    intent_map = {
        "chat": TaskIntent.CHAT,
        "inspect": TaskIntent.INSPECT,
        "design": TaskIntent.DESIGN,
        "review": TaskIntent.REVIEW,
        "implement": TaskIntent.IMPLEMENT,
        "debug": TaskIntent.DEBUG,
    }
    if normalized in intent_map:
        return ToolStatePatch(
            task_intent=intent_map[normalized],
            intent_resolution_reason=f"clarify: user selected {normalized}",
            intent_source="clarify",
            intent_refined=True,
        )

    if "scope" in inp.context.lower():
        return ToolStatePatch(
            goal=answer,
            intent_resolution_reason="clarify: user refined scope",
            intent_source="clarify",
            intent_refined=True,
        )
    return None


def _selected_option(inp: ClarifyInput, response: UserResponse) -> str | None:
    if response.free_text:
        return None
    option_values = {option.value for option in inp.options}
    if response.value in option_values:
        return response.value
    return None
