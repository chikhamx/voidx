"""Structured clarification tool."""

from __future__ import annotations

import json
from uuid import uuid4

from pydantic import BaseModel, Field

from voidx.runtime import IntentResolution, TaskIntent, ToolStatePatch
from voidx.tools.base import (
    BaseTool,
    ToolContext,
    ToolResult,
    UserInteraction,
    UserResponse,
    model_to_json_schema,
)


class ClarifyInput(BaseModel):
    question: str = Field(description="One specific clarifying question to ask the user.")
    options: list[str] = Field(
        default_factory=list,
        description="Suggested mutually exclusive answers; leave empty for an open-ended question.",
    )


class ClarifyResult(BaseModel):
    question: str
    answer: str
    cancelled: bool = False
    state_patch: ToolStatePatch | None = None


class ClarifyTool(BaseTool):
    id = "clarify"
    description = (
        "Ask the user one question when intent, scope, or requirements are ambiguous "
        "and explicit input is needed before proceeding. Do not use for progress updates. "
        "Later tool calls in the same response are deferred until the answer updates runtime state."
    )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(ClarifyInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            inp = ClarifyInput.model_validate(args)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", summary="clarify: invalid arguments", metadata={"error": True})
        if ctx.interact is None:
            return ToolResult(
                title="clarify: unavailable",
                output=(
                    "Clarification is not available in this runtime. "
                    "Proceed only with safe assumptions and note the ambiguity."
                ),
                summary="clarify: unavailable",
                metadata={"clarify_cancelled": True, "blocked": True},
            )

        clarify_id = uuid4().hex
        event_ui_active = _emit_clarify_shown(clarify_id, inp)
        response = await ctx.interact(UserInteraction(
            prompt="Question:" if event_ui_active else inp.question,
            options=[] if event_ui_active else inp.options,
            timeout=120.0,
        ))
        _emit_clarify_answer(clarify_id, response)
        if response.cancelled:
            return ToolResult(
                title="clarify: skipped",
                output=f"User skipped clarification: {inp.question}",
                summary="clarify: skipped",
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
        return ToolStatePatch(
            intent=IntentResolution(type=intent_map[normalized]),
        )

    return None


def _emit_clarify_shown(clarify_id: str, inp: ClarifyInput) -> bool:
    try:
        from voidx.ui.output.events import ui_events
        from voidx.ui.output.events.schema import ClarifyPromptShown
    except ImportError:
        return False
    if not ui_events.is_running:
        return False
    ui_events.emit_direct(ClarifyPromptShown(
        clarify_id=clarify_id,
        question=inp.question,
        options=list(inp.options),
    ))
    return True


def _emit_clarify_answer(clarify_id: str, response: UserResponse) -> None:
    try:
        from voidx.ui.output.events import ui_events
        from voidx.ui.output.events.schema import ClarifyAnswerSubmitted
    except ImportError:
        return
    if not ui_events.is_running:
        return
    ui_events.emit_direct(ClarifyAnswerSubmitted(
        clarify_id=clarify_id,
        answer=response.value,
        cancelled=response.cancelled,
        was_custom_input=True,
    ))
