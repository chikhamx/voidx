from __future__ import annotations

from typing import Any, Literal

from voidx.agent.domain.turn_context import TurnExecutionContext

from pydantic import BaseModel, ConfigDict, Field

from voidx.agent.domain.state import SessionRuntimeState
from voidx.agent.domain.thread import AgentThread, LifecycleState


class TurnRequest(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    thread: AgentThread
    user_text: str
    context: TurnExecutionContext
    display_text: str | None = None
    persist_user_input: bool = True
    runtime: SessionRuntimeState | None = None
    guidance: tuple[dict[str, Any], ...] | None = None


class TurnResult(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    thread: AgentThread
    lifecycle: LifecycleState
    runtime: SessionRuntimeState | None = None
    error: str | None = None
    final_llm_messages: tuple[object, ...] = ()
    final_assistant_summary: str = ""
    tool_result_summaries: tuple[str, ...] = ()
    current_turn_tool_result_summaries: tuple[str, ...] = ()
    stop_signal: str = ""

    @property
    def session_id(self) -> str | None:
        return self.thread.session_id


GoalPhaseDisposition = Literal[
    "committed",
    "retry_same_phase",
    "fallback_committed",
    "needs_user",
    "terminal",
]


class GoalPhaseResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    phase: Literal["work", "evaluator"]
    attempt_number: int = Field(ge=0)
    disposition: GoalPhaseDisposition = "committed"
    protocol_id: str = ""
    needs_resume: bool = False
    reason: str = ""


__all__ = ["GoalPhaseResult", "TurnRequest", "TurnResult"]
