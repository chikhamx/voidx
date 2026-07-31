"""Graph-level protocol registry: each runtime profile declares its turn-lifecycle protocol.

A graph protocol owns the protocol tool set injected into the LLM (turn, loop,
goal, …), classifies assistant messages against that protocol, and decides
whether a turn may end. Profiles select a protocol via
``RuntimeProfile.protocol``; unknown ids fall back to the turn protocol so
existing sessions keep working.
"""

from __future__ import annotations

from typing import Any, Protocol as TypingProtocol

from langchain_core.messages import AIMessage

from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.infrastructure.langgraph.runtime.core.loop import LlmLoopState
from voidx.agent.infrastructure.langgraph.runtime.turn_control import (
    LOOP_DECISION_PROMPT,
    TURN_TOOL_DEFINITION,
    TurnClassification,
    classify_turn_call,
)
from voidx.tools.loop import LoopTool


class GraphProtocol(TypingProtocol):
    """Graph-level protocol tool set for one runtime profile."""

    protocol_id: str

    def tool_definitions(self) -> list[dict[str, Any]]: ...

    def classify(self, msg: AIMessage) -> TurnClassification: ...

    def decision_missing(
        self, msg: AIMessage, loop: LlmLoopState, *, controller: Any | None
    ) -> bool: ...

    def repair_prompt(self) -> str: ...


class TurnToolProtocol:
    """Default coding/chat protocol: turn(start/stop) manages the whole lifecycle."""

    protocol_id = "turn"

    def tool_definitions(self) -> list[dict[str, Any]]:
        return [TURN_TOOL_DEFINITION]

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


def strip_tool_calls_after_loop_commit(msg):
    """Drop tool calls from the final assistant message once the loop committed.

    After operation=commit the iteration is over: executing further tool calls
    would keep the turn alive forever and the scheduled wakeup delay would never
    take effect. Stripping (instead of routing around the tools node) keeps the
    message history free of dangling tool_calls.
    """
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
    # Text + tool calls in one message still must not end the iteration without
    # a loop decision: the model "says" it committed but the commit tool call is
    # never reached when the message is the turn's terminal one.
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

    def classify(self, msg: AIMessage) -> TurnClassification:
        return classify_turn_call(msg)

    def decision_missing(
        self, msg: AIMessage, loop: LlmLoopState, *, controller: Any | None
    ) -> bool:
        if controller is None:
            return False
        if self.classify(msg) not in _BARRIER_CLASSIFICATIONS:
            return False
        if loop.protocol_repairs >= _MAX_DECISION_REPAIRS:
            return False
        # Mid-iteration tool turns are fine; only block when the model adds a
        # closing summary (it believes the iteration is over) without having
        # committed a loop decision.
        from voidx.agent.infrastructure.langgraph.runtime.streaming import extract_text

        if self.classify(msg) is TurnClassification.REGULAR_TOOLS and not extract_text(msg).strip():
            return False
        return controller.final_decision() is None

    def repair_prompt(self) -> str:
        return LOOP_DECISION_PROMPT


class GoalProtocol:
    """Goal protocol: evaluator may verify, then submits one lifecycle decision."""

    protocol_id = "goal"

    def __init__(
        self,
        *,
        verification_tool_ids: set[str] | None = None,
        verification_tool_definitions: list[dict[str, Any]] | None = None,
    ) -> None:
        self.verification_tool_ids = verification_tool_ids or set()
        self.verification_tool_definitions = verification_tool_definitions or []

    def tool_definitions(self) -> list[dict[str, Any]]:
        from voidx.tools.goal import GoalTool

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
        return controller.final_decision() is None

    def repair_prompt(self) -> str:
        return (
            "Evaluate the acceptance condition using policy-approved verification tools when needed, "
            "then call goal with status=finished, continue, or blocked."
        )


_PROTOCOL_TYPES = {
    "turn": TurnToolProtocol,
    "loop": LoopProtocol,
    "goal": GoalProtocol,
}


def resolve_graph_protocol(profile: RuntimeProfile | None) -> GraphProtocol:
    protocol_id = getattr(profile, "protocol", "") or "turn"
    return _PROTOCOL_TYPES.get(protocol_id, TurnToolProtocol)()
