"""Structured clarification tool."""

from __future__ import annotations

import json
from uuid import uuid4

from pydantic import BaseModel, Field

from voidx.agent.adapters.tools.context import AgentToolExecutionContext as ToolContext
from voidx.tooling.domain.result import ToolResult
from voidx.tooling.domain.interaction import (
    UserInteraction,
    UserResponse,
)
from voidx.tooling.domain.schema import model_to_json_schema
from voidx.tooling.domain.ui_events import ClarifyAnswerSubmitted, ClarifyPromptShown, ToolUiEventPublisher


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


class ClarifyTool:
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
        if ctx.runtime.interaction is None:
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
        event_ui_active = _emit_clarify_shown(ctx.runtime.events, clarify_id, inp)
        response = await ctx.runtime.interaction(UserInteraction(
            prompt="Question:" if event_ui_active else inp.question,
            options=[] if event_ui_active else inp.options,
            timeout=120.0,
        ))
        _emit_clarify_answer(ctx.runtime.events, clarify_id, response)
        if response.cancelled:
            return ToolResult(
                title="clarify: skipped",
                output=f"User skipped clarification: {inp.question}",
                summary="clarify: skipped",
                metadata={"clarify_cancelled": True},
            )

        result = ClarifyResult(
            question=inp.question,
            answer=response.value,
        )
        payload = result.model_dump(mode="json")
        metadata = {
            "clarify_answer": response.value,
        }
        return ToolResult(
            title=f"clarify: {response.value}",
            output=json.dumps(payload, ensure_ascii=False, indent=2),
            summary=f"answer: {response.value}",
            metadata=metadata,
        )


def _emit_clarify_shown(
    publisher: ToolUiEventPublisher | None,
    clarify_id: str,
    inp: ClarifyInput,
) -> bool:
    if publisher is None or not publisher.is_running:
        return False
    publisher.emit(ClarifyPromptShown(
        clarify_id=clarify_id,
        question=inp.question,
        options=list(inp.options),
    ))
    return True


def _emit_clarify_answer(
    publisher: ToolUiEventPublisher | None,
    clarify_id: str,
    response: UserResponse,
) -> None:
    if publisher is None or not publisher.is_running:
        return
    publisher.emit(ClarifyAnswerSubmitted(
        clarify_id=clarify_id,
        answer=response.value,
        cancelled=response.cancelled,
        was_custom_input=True,
    ))
