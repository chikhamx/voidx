"""Graph-owned turn initialization control tool.

The ``turn_init`` tool is a protocol signal, not a normal runtime tool. It is
intercepted inside ``_call_llm`` before tool authorization or execution. It
never creates a ``ToolMessage`` and is not registered in ``ToolRegistry``.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from langchain_core.messages import AIMessage

TURN_TOOL_NAME = "turn_init"


TURN_TOOL_DEFINITION: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": TURN_TOOL_NAME,
        "description": (
            "Initialize the turn with a short goal. "
            "Call turn_init once at the beginning of a turn; it may be combined "
            "with regular tools in the same response, and initialization is applied first."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {"goal": {"type": "string"}},
            "required": ["goal"],
            "additionalProperties": False,
        },
    },
}


class TurnClassification(str, Enum):
    VALID_INIT = "valid_init"
    VALID_INIT_WITH_TOOLS = "valid_init_with_tools"
    REGULAR_TOOLS = "regular_tools"
    INVALID_TURN = "invalid_turn"
    PLAIN_TEXT = "plain_text"


TURN_INIT_PROMPT = (
    "Turn state is initial. Do not output text yet. "
    "Call turn_init with a short goal now."
)

FIRST_MISS_PROMPT = (
    "If you still need to work, call a regular tool instead of outputting text. "
    "When the final answer is ready, output it as plain text."
)

SECOND_MISS_PROMPT = FIRST_MISS_PROMPT

INVALID_TURN_PROMPT = (
    "Use only the bound turn_init({goal}) call to initialize the turn, or use a regular tool."
)

LOOP_DECISION_PROMPT = (
    "This is a /loop iteration. The turn cannot end until you submit the iteration "
    "decision with loop_commit: outcome='continue' and summary='...'."
)


def _has_tool_calls(msg: AIMessage) -> bool:
    calls = getattr(msg, "tool_calls", None)
    return bool(calls)


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


def _extract_goal_from_args(args: Any, *, _depth: int = 0) -> str | None:
    if _depth > 3 or not isinstance(args, dict):
        return None
    goal = args.get("goal")
    if isinstance(goal, str) and goal.strip():
        return goal.strip()
    for key in ("objective", "task", "description", "target", "query", "prompt"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    for key in ("params", "parameters", "arguments", "input"):
        nested = args.get(key)
        if isinstance(nested, dict):
            nested_goal = _extract_goal_from_args(nested, _depth=_depth + 1)
            if nested_goal:
                return nested_goal
    return None


def _valid_init_args(args: Any) -> bool:
    return bool(_extract_goal_from_args(args))


def classify_turn_call(msg: AIMessage) -> TurnClassification:
    calls = getattr(msg, "tool_calls", None) or []
    if not calls:
        return TurnClassification.PLAIN_TEXT
    if any(not isinstance(call, dict) for call in calls):
        return TurnClassification.INVALID_TURN

    names = [str(call.get("name") or "") for call in calls]
    if "turn" in names:
        return TurnClassification.INVALID_TURN
    turn_count = sum(1 for name in names if name == TURN_TOOL_NAME)
    regular_count = len(calls) - turn_count

    if turn_count == 0:
        return TurnClassification.REGULAR_TOOLS
    if turn_count != 1:
        return TurnClassification.INVALID_TURN

    turn_call = next(call for call in calls if str(call.get("name") or "") == TURN_TOOL_NAME)
    if not _valid_init_args(turn_call.get("args")):
        return TurnClassification.INVALID_TURN
    if regular_count:
        return TurnClassification.VALID_INIT_WITH_TOOLS
    return TurnClassification.VALID_INIT


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


# Kept as a small compatibility-neutral helper for callers that need to
# recognize a terminal candidate while migrating from the old stop barrier.
def _has_text(msg: AIMessage | None) -> bool:
    return msg is not None and _is_non_empty_text(getattr(msg, "content", None))
