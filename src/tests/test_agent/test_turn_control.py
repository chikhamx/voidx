"""Tests for turn control tool: schema, classification, validation, normalization."""

from langchain_core.messages import AIMessage

from voidx.agent.graph.turn_control import (
    FIRST_MISS_PROMPT,
    INVALID_TURN_PROMPT,
    SECOND_MISS_PROMPT,
    TURN_TOOL_DEFINITION,
    TurnClassification,
    classify_turn_call,
    normalize_terminal_message,
    validate_turn_call,
)


def _ai_with_turn_call() -> AIMessage:
    return AIMessage(
        content="Here is the answer.",
        tool_calls=[{"name": "turn", "args": {}, "id": "call_1", "type": "tool_call"}],
    )


def _ai_with_regular_tool_call() -> AIMessage:
    return AIMessage(
        content="Let me read that file.",
        tool_calls=[{"name": "read", "args": {"file_path": "x.py"}, "id": "call_2", "type": "tool_call"}],
    )


def _ai_with_mixed_calls() -> AIMessage:
    return AIMessage(
        content="Done after reading.",
        tool_calls=[
            {"name": "read", "args": {"file_path": "x.py"}, "id": "call_3", "type": "tool_call"},
            {"name": "turn", "args": {}, "id": "call_4", "type": "tool_call"},
        ],
    )


def _ai_plain_text(text: str = "Here is the answer.") -> AIMessage:
    return AIMessage(content=text)


# ── Schema ──────────────────────────────────────────────────────────────────


def test_turn_tool_definition_has_correct_name():
    assert TURN_TOOL_DEFINITION["function"]["name"] == "turn"


def test_turn_tool_definition_description_requires_turn():
    description = TURN_TOOL_DEFINITION["function"]["description"]
    assert "completed your response to the user's request" in description
    assert "call this" in description
    assert "only tool" in description
    assert "end the turn" in description


def test_turn_tool_definition_has_empty_parameters():
    params = TURN_TOOL_DEFINITION["function"]["parameters"]
    assert params["type"] == "object"
    assert params["properties"] == {}
    assert params["required"] == []
    assert params["additionalProperties"] is False


def test_turn_tool_definition_is_strict():
    assert TURN_TOOL_DEFINITION["function"]["strict"] is True


# ── Classification ───────────────────────────────────────────────────────────


def test_classify_valid_turn_call():
    msg = _ai_with_turn_call()
    assert classify_turn_call(msg) == TurnClassification.VALID_TURN


def test_classify_regular_tool_call():
    msg = _ai_with_regular_tool_call()
    assert classify_turn_call(msg) == TurnClassification.REGULAR_TOOLS


def test_classify_mixed_turn_and_regular():
    msg = _ai_with_mixed_calls()
    assert classify_turn_call(msg) == TurnClassification.INVALID_TURN


def test_classify_plain_text_no_tool_calls():
    msg = _ai_plain_text()
    assert classify_turn_call(msg) == TurnClassification.PLAIN_TEXT


def test_classify_empty_message():
    msg = AIMessage(content="")
    assert classify_turn_call(msg) == TurnClassification.PLAIN_TEXT


# ── Validation ──────────────────────────────────────────────────────────────


def test_validate_turn_call_valid_with_pending():
    msg = _ai_with_turn_call()
    pending = _ai_plain_text("Provisional answer.")
    assert validate_turn_call(msg, pending) is True


def test_validate_turn_call_rejected_without_pending():
    msg = _ai_with_turn_call()
    assert validate_turn_call(msg, None) is False


def test_validate_turn_call_rejected_with_empty_pending():
    msg = _ai_with_turn_call()
    pending = AIMessage(content="")
    assert validate_turn_call(msg, pending) is False


def test_validate_turn_call_rejected_with_whitespace_pending():
    msg = _ai_with_turn_call()
    pending = AIMessage(content="   \n  ")
    assert validate_turn_call(msg, pending) is False


# ── Normalization ───────────────────────────────────────────────────────────


def test_normalize_terminal_message_strips_tool_calls():
    pending = AIMessage(
        content="Final answer.",
        additional_kwargs={
            "tool_calls": [{"name": "turn", "args": {}, "id": "call_1"}],
            "response_metadata": {"model": "gpt-4", "finish_reason": "tool_calls"},
        },
    )
    terminal = normalize_terminal_message(pending)
    assert isinstance(terminal, AIMessage)
    assert terminal.content == "Final answer."
    assert "tool_calls" not in terminal.additional_kwargs
    assert terminal.additional_kwargs["response_metadata"]["model"] == "gpt-4"


def test_normalize_terminal_message_preserves_content():
    pending = AIMessage(content="Multi\nline\nanswer.")
    terminal = normalize_terminal_message(pending)
    assert terminal.content == "Multi\nline\nanswer."


# ── Repair prompts ──────────────────────────────────────────────────────────


def test_repair_prompts_are_non_empty():
    assert len(FIRST_MISS_PROMPT.strip()) > 10
    assert len(SECOND_MISS_PROMPT.strip()) > 10
    assert len(INVALID_TURN_PROMPT.strip()) > 10


def test_first_miss_prompt_mentions_turn():
    assert "turn" in FIRST_MISS_PROMPT.lower()
    assert "if yes" in FIRST_MISS_PROMPT.lower()
    assert "if no" in FIRST_MISS_PROMPT.lower()
    assert "user's request" in FIRST_MISS_PROMPT.lower()


def test_second_miss_prompt_mentions_turn():
    assert "turn" in SECOND_MISS_PROMPT.lower()


def test_invalid_turn_prompt_mentions_separate_step():
    assert "separate" in INVALID_TURN_PROMPT.lower() or "first" in INVALID_TURN_PROMPT.lower()
