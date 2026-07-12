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
            "Turn lifecycle control. At turn start, call operation='start' with intent and a short goal before other work. "
            "At turn end, call operation='stop' only after the pending final answer is complete. "
            "Do not combine turn with other tool calls. Do not output text with this call."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["start", "stop"],
                    "description": "start declares intent and goal; stop commits the pending final answer.",
                },
                "intent": {
                    "type": "string",
                    "enum": ["coding", "general", ""],
                    "description": "For start: coding=code task, general=chat/Q&A. For stop: pass empty string.",
                },
                "goal": {
                    "type": "string",
                    "description": "For start: short stable objective for this user turn. For stop: pass empty string.",
                },
            },
            "required": ["operation", "intent", "goal"],
            "additionalProperties": False,
        },
    },
}


class TurnClassification(str, Enum):
    VALID_TURN = "valid_turn"
    VALID_START = "valid_start"
    REGULAR_TOOLS = "regular_tools"
    INVALID_TURN = "invalid_turn"
    PLAIN_TEXT = "plain_text"


TURN_STOP_PROMPT = (
    "Do not output text. If finished, call turn with operation='stop', intent='', and goal='' now. "
    "If not finished, continue with a regular tool instead."
)

TURN_START_PROMPT = (
    "Turn state is initial. Do not output text yet. Call turn with operation='start', intent, and a short goal now."
)

FIRST_MISS_PROMPT = TURN_STOP_PROMPT

SECOND_MISS_PROMPT = TURN_STOP_PROMPT

INVALID_TURN_PROMPT = (
    "The turn control response was invalid. Do not output text. Call turn with "
    "operation='stop', intent='', and goal='' to commit the pending response, or call a regular tool to continue working."
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
        if not isinstance(args, dict) or set(args) != {"operation", "intent", "goal"}:
            return TurnClassification.INVALID_TURN
        operation = args.get("operation")
        intent = args.get("intent")
        goal = args.get("goal")
        if operation == "stop":
            return TurnClassification.VALID_TURN
        if operation == "start" and intent in {"coding", "general"} and _is_non_empty_text(goal):
            return TurnClassification.VALID_START

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
