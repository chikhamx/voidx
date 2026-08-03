"""Tests for turn control tool: schema, classification, validation, normalization."""

import pytest
from langchain_core.messages import AIMessage

from voidx.agent.infrastructure.langgraph.runtime.turn_control import (
    FIRST_MISS_PROMPT,
    NO_USER_RESPONSE_PROMPT,
    SECOND_MISS_PROMPT,
    TURN_START_PROMPT,
    TURN_TOOL_DEFINITION,
    TurnClassification,
    classify_turn_call,
    normalize_terminal_message,
    validate_turn_call,
)


def _ai_with_turn_stop() -> AIMessage:
    return AIMessage(
        content="Here is the answer.",
        tool_calls=[{
            "name": "turn",
            "args": {"operation": "stop", "params": None},
            "id": "call_1",
            "type": "tool_call",
        }],
    )


def _ai_with_turn_start(intent: str = "coding", goal: str = "Fix the bug") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{
            "name": "turn",
            "args": {"operation": "start", "params": {"intent": intent, "goal": goal}},
            "id": "call_start",
            "type": "tool_call",
        }],
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
            {"name": "turn", "args": {"operation": "stop", "params": None}, "id": "call_4", "type": "tool_call"},
        ],
    )


def _ai_with_start_and_regular_tools() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "turn",
                "args": {"operation": "start", "params": {"intent": "coding", "goal": "Inspect file"}},
                "id": "call_start_mixed",
                "type": "tool_call",
            },
            {"name": "read", "args": {"file_path": "x.py"}, "id": "call_read_mixed", "type": "tool_call"},
        ],
    )


def _ai_plain_text(text: str = "Here is the answer.") -> AIMessage:
    return AIMessage(content=text)


# ── Schema ──────────────────────────────────────────────────────────────────


def test_turn_tool_definition_has_correct_name():
    assert TURN_TOOL_DEFINITION["function"]["name"] == "turn"


def test_turn_tool_definition_description_requires_start_and_stop():
    description = TURN_TOOL_DEFINITION["function"]["description"]
    assert "operation='start'" in description
    assert "operation='stop'" in description
    assert "intent and a short goal" in description
    assert "At turn end" in description


def test_turn_tool_definition_requires_operation_intent_goal():
    params = TURN_TOOL_DEFINITION["function"]["parameters"]
    assert params["type"] == "object"
    assert params["properties"]["operation"]["enum"] == ["start", "stop"]
    assert params["properties"]["params"]["anyOf"][0]["properties"]["intent"]["enum"] == ["coding", "general"]
    assert "goal" in params["properties"]["params"]["anyOf"][0]["properties"]
    assert params["required"] == ["operation", "params"]
    assert params["additionalProperties"] is False


def test_turn_tool_definition_is_strict():
    assert TURN_TOOL_DEFINITION["function"]["strict"] is True


# ── Classification ───────────────────────────────────────────────────────────


def test_classify_valid_turn_stop_call():
    msg = _ai_with_turn_stop()
    assert classify_turn_call(msg) == TurnClassification.VALID_TURN


def test_classify_turn_stop_without_params_key():
    msg = AIMessage(
        content="",
        tool_calls=[{
            "name": "turn",
            "args": {"operation": "stop"},
            "id": "call_no_params",
            "type": "tool_call",
        }],
    )
    assert classify_turn_call(msg) == TurnClassification.VALID_TURN


def test_classify_valid_turn_start_call():
    msg = _ai_with_turn_start()
    assert classify_turn_call(msg) == TurnClassification.VALID_START


def test_classify_turn_start_accepts_general_intent():
    msg = _ai_with_turn_start(intent="general", goal="Answer the question")
    assert classify_turn_call(msg) == TurnClassification.VALID_START


def test_classify_turn_start_rejects_empty_goal():
    msg = _ai_with_turn_start(goal="  ")
    assert classify_turn_call(msg) == TurnClassification.INVALID_TURN


def test_classify_turn_start_rejects_empty_or_invalid_intent():
    assert classify_turn_call(_ai_with_turn_start(intent="")) == TurnClassification.INVALID_TURN
    assert classify_turn_call(_ai_with_turn_start(intent="debug")) == TurnClassification.INVALID_TURN


def test_classify_turn_stop_ignores_empty_sentinel_fields():
    msg = _ai_with_turn_stop()
    assert classify_turn_call(msg) == TurnClassification.VALID_TURN


def test_classify_legacy_decision_turn_call_invalid():
    msg = AIMessage(
        content="",
        tool_calls=[{"name": "turn", "args": {"decision": "stop"}, "id": "call_legacy", "type": "tool_call"}],
    )
    assert classify_turn_call(msg) == TurnClassification.INVALID_TURN


def test_classify_legacy_empty_turn_call_invalid():
    msg = AIMessage(
        content="",
        tool_calls=[{"name": "turn", "args": {}, "id": "call_legacy", "type": "tool_call"}],
    )
    assert classify_turn_call(msg) == TurnClassification.INVALID_TURN


def test_classify_turn_rejects_multiple_turn_calls_without_treating_them_as_valid():
    msg = AIMessage(
        content="",
        tool_calls=[
            {"name": "turn", "args": {}, "id": "call_empty", "type": "tool_call"},
            {"name": "turn", "args": {}, "id": "call_empty_2", "type": "tool_call"},
        ],
    )
    assert classify_turn_call(msg) == TurnClassification.INVALID_TURN


@pytest.mark.asyncio
async def test_invalid_turn_is_repaired_twice_before_failing():
    from types import SimpleNamespace
    from voidx.agent.infrastructure.langgraph.runtime.core.loop import LlmLoopState
    from voidx.agent.infrastructure.langgraph.runtime.core.turn import handle_turn_control_response
    from voidx.runtime.task_state import TaskState

    graph = SimpleNamespace(_turn_metrics=SimpleNamespace(increment=lambda _name: None))
    loop = LlmLoopState(context_tokens=0)
    runtime_task_state = TaskState()
    assistant = AIMessage(
        content="",
        tool_calls=[{"name": "turn", "args": {}, "id": "bad", "type": "tool_call"}],
    )

    first = await handle_turn_control_response(
        graph=graph, assistant_msg=assistant, llm_messages=[], loop=loop,
        turn_state="running", runtime_task_state=runtime_task_state,
        state_messages=[], interaction_mode_value="auto", estimate_tokens=len,
        rerender_task_context=lambda messages, _state, _task: messages,
    )
    second = await handle_turn_control_response(
        graph=graph, assistant_msg=assistant, llm_messages=first.llm_messages, loop=loop,
        turn_state="running", runtime_task_state=runtime_task_state,
        state_messages=[], interaction_mode_value="auto", estimate_tokens=len,
        rerender_task_context=lambda messages, _state, _task: messages,
    )
    third = await handle_turn_control_response(
        graph=graph, assistant_msg=assistant, llm_messages=second.llm_messages, loop=loop,
        turn_state="running", runtime_task_state=runtime_task_state,
        state_messages=[], interaction_mode_value="auto", estimate_tokens=len,
        rerender_task_context=lambda messages, _state, _task: messages,
    )

    assert first.action == "retry"
    assert second.action == "retry"
    assert third.action == "fail"


def test_classify_regular_tool_call():
    msg = _ai_with_regular_tool_call()
    assert classify_turn_call(msg) == TurnClassification.REGULAR_TOOLS


def test_classify_mixed_turn_stop_and_regular_with_text():
    msg = _ai_with_mixed_calls()
    assert classify_turn_call(msg) == TurnClassification.VALID_STOP_WITH_TOOLS


def test_classify_mixed_turn_stop_and_regular_without_text():
    msg = AIMessage(
        content="",
        tool_calls=[
            {"name": "read", "args": {"file_path": "x.py"}, "id": "call_3", "type": "tool_call"},
            {"name": "turn", "args": {"operation": "stop", "params": None}, "id": "call_4", "type": "tool_call"},
        ],
    )
    assert classify_turn_call(msg) == TurnClassification.INVALID_TURN


def test_classify_start_with_regular_tools():
    msg = _ai_with_start_and_regular_tools()
    assert classify_turn_call(msg) == TurnClassification.VALID_START_WITH_TOOLS


def test_classify_start_with_regular_tools_order_independent():
    msg = AIMessage(
        content="",
        tool_calls=[
            {"name": "read", "args": {"file_path": "x.py"}, "id": "call_read_first", "type": "tool_call"},
            {
                "name": "turn",
                "args": {"operation": "start", "params": {"intent": "coding", "goal": "Inspect file"}},
                "id": "call_start_second",
                "type": "tool_call",
            },
        ],
    )
    assert classify_turn_call(msg) == TurnClassification.VALID_START_WITH_TOOLS


def test_classify_stop_with_regular_tools_order_independent():
    msg = AIMessage(
        content="Done after reading.",
        tool_calls=[
            {"name": "turn", "args": {"operation": "stop", "params": None}, "id": "call_stop_first", "type": "tool_call"},
            {"name": "read", "args": {"file_path": "x.py"}, "id": "call_read_second", "type": "tool_call"},
        ],
    )
    assert classify_turn_call(msg) == TurnClassification.VALID_STOP_WITH_TOOLS


def test_classify_multiple_turn_calls_invalid():
    msg = AIMessage(
        content="Done.",
        tool_calls=[
            {"name": "turn", "args": {"operation": "stop", "params": None}, "id": "call_stop_1", "type": "tool_call"},
            {"name": "turn", "args": {"operation": "start", "params": {"intent": "coding", "goal": "x"}}, "id": "call_start_2", "type": "tool_call"},
        ],
    )
    assert classify_turn_call(msg) == TurnClassification.INVALID_TURN


def test_classify_start_with_tools_rejects_empty_goal():
    msg = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "turn",
                "args": {"operation": "start", "params": {"intent": "coding", "goal": "  "}},
                "id": "call_start_empty",
                "type": "tool_call",
            },
            {"name": "read", "args": {"file_path": "x.py"}, "id": "call_read", "type": "tool_call"},
        ],
    )
    assert classify_turn_call(msg) == TurnClassification.INVALID_TURN


def test_classify_plain_text_no_tool_calls():
    msg = _ai_plain_text()
    assert classify_turn_call(msg) == TurnClassification.PLAIN_TEXT


def test_classify_empty_message():
    msg = AIMessage(content="")
    assert classify_turn_call(msg) == TurnClassification.PLAIN_TEXT


# ── Validation ──────────────────────────────────────────────────────────────


def test_validate_turn_call_valid_with_pending():
    msg = _ai_with_turn_stop()
    pending = _ai_plain_text("Provisional answer.")
    assert validate_turn_call(msg, pending) is True


def test_validate_turn_call_rejected_without_pending():
    msg = _ai_with_turn_stop()
    assert validate_turn_call(msg, None) is False


def test_validate_turn_call_rejected_with_empty_pending():
    msg = _ai_with_turn_stop()
    pending = AIMessage(content="")
    assert validate_turn_call(msg, pending) is False


def test_validate_turn_call_rejected_with_whitespace_pending():
    msg = _ai_with_turn_stop()
    pending = AIMessage(content="   \n  ")
    assert validate_turn_call(msg, pending) is False


def test_validate_turn_call_rejects_start_even_with_pending():
    msg = _ai_with_turn_start()
    pending = _ai_plain_text("Provisional answer.")
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
    assert len(NO_USER_RESPONSE_PROMPT.strip()) > 10


def test_first_miss_prompt_mentions_turn():
    prompt = FIRST_MISS_PROMPT.lower()
    assert "turn" in prompt
    assert "operation='stop'" in prompt
    assert "regular tool" in prompt
    assert "final answer" in prompt
    assert "still need to work" in prompt


def test_start_prompt_mentions_start_intent_and_goal():
    prompt = TURN_START_PROMPT.lower()
    assert "turn" in prompt
    assert "operation='start'" in prompt
    assert "intent" in prompt
    assert "goal" in prompt


def test_second_miss_prompt_mentions_turn():
    assert "turn" in SECOND_MISS_PROMPT.lower()


def test_no_user_response_prompt_mentions_operation_stop():
    prompt = NO_USER_RESPONSE_PROMPT.lower()
    assert "operation='stop'" in prompt
    assert "summary" in prompt
