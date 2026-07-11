"""Graph-owned turn control tool: explicit completion barrier signal.

The ``turn`` tool is a protocol signal, not a normal runtime tool. It is
intercepted inside ``_call_llm`` before tool authorization or execution.
It never creates a ``ToolMessage`` and is not registered in ``ToolRegistry``.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from langchain_core.messages import AIMessage

TURN_TOOL_NAME = "turn"

TURN_TOOL_DEFINITION: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": TURN_TOOL_NAME,
        "description": (
            "Commit the latest assistant response and end the current user turn. "
            "After you have completed your response to the user's request, call this "
            "as the only tool to end the turn. Do not call with any other tool."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
}


class TurnClassification(str, Enum):
    VALID_TURN = "valid_turn"
    REGULAR_TOOLS = "regular_tools"
    INVALID_TURN = "invalid_turn"
    PLAIN_TEXT = "plain_text"


FIRST_MISS_PROMPT = (
    "Decide whether your response to the user's request is complete. "
    "If yes, call turn() now as the only tool to end this turn. "
    "If no, continue working on the user's request using the necessary tools."
)

SECOND_MISS_PROMPT = (
    "Final completion check: do not return another standalone assistant response. "
    "Either call a regular tool to continue required work, or call turn() as the "
    "only tool to commit the latest response and finish this turn."
)

INVALID_TURN_PROMPT = (
    "The turn control call was invalid. Call regular tools first in a separate "
    "assistant step. When all work is complete, call turn as the only tool and "
    "provide the complete response."
)


def _has_tool_calls(msg: AIMessage) -> bool:
    calls = getattr(msg, "tool_calls", None)
    return bool(calls)


def classify_turn_call(msg: AIMessage) -> TurnClassification:
    calls = getattr(msg, "tool_calls", None) or []
    if not calls:
        return TurnClassification.PLAIN_TEXT
    if any(not isinstance(call, dict) for call in calls):
        return TurnClassification.INVALID_TURN

    names = [str(call.get("name") or "") for call in calls]
    turn_count = sum(1 for name in names if name == TURN_TOOL_NAME)
    regular_count = len(calls) - turn_count

    if turn_count == 0:
        return TurnClassification.REGULAR_TOOLS

    if turn_count == 1 and regular_count == 0:
        args = calls[0].get("args")
        if isinstance(args, dict) and not args:
            return TurnClassification.VALID_TURN

    return TurnClassification.INVALID_TURN


def _is_non_empty_text(text: Any) -> bool:
    if text is None:
        return False
    if isinstance(text, str):
        return bool(text.strip())
    if isinstance(text, list):
        return any(
            isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text", ""), str)
            and item["text"].strip()
            for item in text
        )
    return False


def validate_turn_call(msg: AIMessage, pending: AIMessage | None) -> bool:
    if classify_turn_call(msg) != TurnClassification.VALID_TURN:
        return False
    if pending is None:
        return False
    return _is_non_empty_text(pending.content)


def normalize_terminal_message(pending: AIMessage) -> AIMessage:
    return pending.model_copy(update={
        "additional_kwargs": {
            key: value
            for key, value in pending.additional_kwargs.items()
            if key != "tool_calls"
        },
        "tool_calls": [],
        "invalid_tool_calls": [],
    })
