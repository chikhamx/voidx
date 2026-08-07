"""Shared control protocol registry for runtime lifecycle handling."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol as TypingProtocol, Sequence

from langchain_core.messages import AIMessage, BaseMessage

from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.infrastructure.langgraph.runtime.core.loop import LlmLoopState
from voidx.agent.infrastructure.langgraph.runtime.turn_control import (
    LOOP_DECISION_PROMPT,
    TURN_TOOL_DEFINITION,
    TurnClassification,
    classify_turn_call,
)
from voidx.agent.domain.task.state import TaskState
from voidx.agent.adapters.tools.automation.loop import LoopTool


@dataclass(frozen=True)
class ControlContext:
    runtime_profile: RuntimeProfile | None = None
    turn_context: TurnExecutionContext | None = None
    interaction_mode: str = ""
    turn_state: str = ""
    loop_state: LlmLoopState | None = None
    runtime_task_state: TaskState | None = None
    state_messages: Sequence[BaseMessage] = field(default_factory=tuple)
    tool_definitions: Sequence[dict[str, Any]] = field(default_factory=tuple)


class ControlProtocol(TypingProtocol):
    protocol_id: str

    def tool_definitions(self) -> list[dict[str, Any]]: ...

    def controller(self, ctx: ControlContext | TurnExecutionContext | None) -> Any | None: ...

    def classify(self, msg: AIMessage) -> TurnClassification: ...

    def decision_missing(
        self, msg: AIMessage, loop: LlmLoopState, *, controller: Any | None
    ) -> bool: ...

    def repair_prompt(self) -> str: ...


def turn_context_from(ctx: ControlContext | TurnExecutionContext | None) -> TurnExecutionContext | None:
    if ctx is None:
        return None
    if isinstance(ctx, ControlContext):
        return ctx.turn_context
    return ctx


class TurnToolProtocol:
    """Default coding/chat protocol: turn(start/stop) manages the whole lifecycle."""

    protocol_id = "turn"

    def tool_definitions(self) -> list[dict[str, Any]]:
        return [TURN_TOOL_DEFINITION]

    def controller(self, ctx: ControlContext | TurnExecutionContext | None) -> Any | None:
        return None

    def classify(self, msg: AIMessage) -> TurnClassification:
        return classify_turn_call(msg)

    def decision_missing(
        self, msg: AIMessage, loop: LlmLoopState, *, controller: Any | None
    ) -> bool:
        return False

    def repair_prompt(self) -> str:
        return ""


def loop_decision_submitted() -> bool:
    """True when the active loop turn already has a committed iteration decision."""
    from voidx.agent.infrastructure.langgraph.runtime.thread_context import (
        current_thread_execution_state,
    )

    state = current_thread_execution_state()
    turn_context = getattr(state, "turn_context", None) if state else None
    controller = getattr(turn_context, "loop_controller", None) if turn_context else None
    if controller is None:
        return False
    return controller.final_decision() is not None


def strip_tool_calls_after_loop_commit(msg: AIMessage) -> AIMessage:
    """Drop tool calls from the final assistant message once the loop committed."""
    if not loop_decision_submitted():
        return msg
    tool_calls = getattr(msg, "tool_calls", None)
    if not tool_calls:
        return msg
    update = {"tool_calls": []}
    if getattr(msg, "invalid_tool_calls", None):
        update["invalid_tool_calls"] = []
    return msg.model_copy(update=update)


_BARRIER_CLASSIFICATIONS = {
    TurnClassification.VALID_TURN,
    TurnClassification.PLAIN_TEXT,
    TurnClassification.REGULAR_TOOLS,
}
_MAX_DECISION_REPAIRS = 2


class LoopProtocol:
    """Loop protocol: the loop tool replaces turn; a submitted decision ends the iteration."""

    protocol_id = "loop"

    def tool_definitions(self) -> list[dict[str, Any]]:
        schema = LoopTool().parameters_schema()
        return [{
            "type": "function",
            "function": {
                "name": "loop",
                "description": LoopTool.description,
                "parameters": schema,
            },
        }]

    def controller(self, ctx: ControlContext | TurnExecutionContext | None) -> Any | None:
        turn_context = turn_context_from(ctx)
        if turn_context is None:
            return None
        if getattr(turn_context, "loop_phase", "") == "idle":
            return getattr(turn_context, "loop_intake_controller", None)
        return getattr(turn_context, "loop_controller", None)

    def classify(self, msg: AIMessage) -> TurnClassification:
        return classify_turn_call(msg)

    def decision_missing(
        self, msg: AIMessage, loop: LlmLoopState, *, controller: Any | None
    ) -> bool:
        if controller is None:
            return False
        if not hasattr(controller, "final_decision"):
            return False
        if self.classify(msg) not in _BARRIER_CLASSIFICATIONS:
            return False
        if loop.protocol_repairs >= _MAX_DECISION_REPAIRS:
            return False
        from voidx.agent.infrastructure.langgraph.runtime.streaming import extract_text

        if self.classify(msg) is TurnClassification.REGULAR_TOOLS and not extract_text(msg).strip():
            return False
        return controller.final_decision() is None

    def repair_prompt(self) -> str:
        return LOOP_DECISION_PROMPT


class GoalProtocol:
    """Goal protocol: intake initializes goals; evaluator submits lifecycle decisions."""

    protocol_id = "goal"

    def __init__(
        self,
        *,
        phase: str = "work",
        verification_tool_ids: set[str] | None = None,
        verification_tool_definitions: list[dict[str, Any]] | None = None,
    ) -> None:
        self.phase = phase
        self.verification_tool_ids = verification_tool_ids or set()
        self.verification_tool_definitions = verification_tool_definitions or []

    def tool_definitions(self) -> list[dict[str, Any]]:
        from voidx.agent.adapters.tools.automation.goal import GoalTool

        definitions = [{
            "type": "function",
            "function": {
                "name": "goal",
                "description": GoalTool.description,
                "parameters": GoalTool().parameters_schema(),
            },
        }, *self.verification_tool_definitions]
        existing = {item.get("function", {}).get("name") for item in definitions}
        for tool_id in sorted(self.verification_tool_ids - existing):
            definitions.append({
                "type": "function",
                "function": {
                    "name": tool_id,
                    "description": f"Policy-approved verification tool: {tool_id}.",
                    "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
                },
            })
        return definitions

    def controller(self, ctx: ControlContext | TurnExecutionContext | None) -> Any | None:
        turn_context = turn_context_from(ctx)
        if turn_context is None:
            return None
        self.phase = turn_context.goal_phase
        if self.phase == "intake":
            return turn_context.goal_intake_controller
        if self.phase == "evaluator":
            return turn_context.goal_controller
        return None

    def classify(self, msg: AIMessage) -> TurnClassification:
        return classify_turn_call(msg)

    def decision_missing(
        self, msg: AIMessage, loop: LlmLoopState, *, controller: Any | None
    ) -> bool:
        if controller is None or self.classify(msg) not in _BARRIER_CLASSIFICATIONS:
            return False
        if loop.protocol_repairs >= _MAX_DECISION_REPAIRS:
            return False
        from voidx.agent.infrastructure.langgraph.runtime.streaming import extract_text

        if self.classify(msg) is TurnClassification.REGULAR_TOOLS and not extract_text(msg).strip():
            return False
        if self.phase == "intake":
            if getattr(controller, "cancelled", False):
                return False
            return controller.final_spec() is None
        if self.phase == "evaluator":
            return controller.final_decision() is None
        return False

    def repair_prompt(self) -> str:
        if self.phase == "intake":
            return (
                "Do not execute the user's request; this stage only defines the goal. "
                "Ask one concise clarification question if needed, otherwise call goal with "
                "op=\"init\", objective, acceptance_condition, and optional achievement_method. "
                "The spec is shown to the user for approval; revise and re-submit if they request changes."
            )
        return (
            "Evaluate the acceptance condition using policy-approved verification tools when needed, "
            "then call goal with op=\"decision\" and status=\"finished\", \"continue\", or \"blocked\"."
        )


_PROTOCOL_TYPES: dict[str, type[ControlProtocol]] = {
    "turn": TurnToolProtocol,
    "loop": LoopProtocol,
    "goal": GoalProtocol,
}


def resolve_control_protocol(profile: RuntimeProfile | None) -> ControlProtocol:
    protocol_id = getattr(profile, "protocol", "") or "turn"
    return _PROTOCOL_TYPES.get(protocol_id, TurnToolProtocol)()
