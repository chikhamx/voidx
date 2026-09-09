"""Tests for the turn_init control tool and plain-text terminal protocol."""

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from voidx.agent.adapters.langgraph.runtime.core.loop import LlmLoopState
from voidx.agent.adapters.langgraph.runtime.core.turn import handle_turn_control_response
from voidx.agent.adapters.langgraph.runtime.turn_control import (
    FIRST_MISS_PROMPT,
    INVALID_TURN_PROMPT,
    LOOP_DECISION_PROMPT,
    SECOND_MISS_PROMPT,
    TURN_INIT_PROMPT,
    TURN_TOOL_DEFINITION,
    TurnClassification,
    classify_turn_call,
    normalize_terminal_message,
)
from voidx.agent.domain.task.state import TaskState


def _call(name: str, args: dict, call_id: str = "call_1") -> dict:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _ai_with_turn_init(goal: str = "Fix the bug") -> AIMessage:
    return AIMessage(content="", tool_calls=[_call("turn_init", {"goal": goal})])


def _ai_with_regular_tool_call() -> AIMessage:
    return AIMessage(
        content="Let me read that file.",
        tool_calls=[_call("read", {"file_path": "x.py"}, "call_read")],
    )


def _ai_plain_text(text: str = "Here is the answer.") -> AIMessage:
    return AIMessage(content=text)


# ── Schema ──────────────────────────────────────────────────────────────────


def test_turn_init_tool_definition_uses_flat_goal_schema():
    assert TURN_TOOL_DEFINITION["function"]["name"] == "turn_init"
    assert TURN_TOOL_DEFINITION["function"]["parameters"] == {
        "type": "object",
        "properties": {"goal": {"type": "string"}},
        "required": ["goal"],
        "additionalProperties": False,
    }
    assert TURN_TOOL_DEFINITION["function"]["strict"] is True


def test_turn_init_tool_description_has_no_stop_semantics():
    description = TURN_TOOL_DEFINITION["function"]["description"].lower()
    assert "turn_init" in description
    assert "goal" in description
    assert "plain text" not in description
    assert "when finished" not in description
    assert "stop" not in description
    assert "operation" not in description


# ── Classification ───────────────────────────────────────────────────────────


def test_classify_turn_init_call():
    assert classify_turn_call(_ai_with_turn_init()) == TurnClassification.VALID_INIT


def test_classify_turn_init_with_regular_tools():
    msg = AIMessage(
        content="",
        tool_calls=[
            _call("turn_init", {"goal": "Inspect file"}, "call_init"),
            _call("read", {"file_path": "x.py"}, "call_read"),
        ],
    )
    assert classify_turn_call(msg) == TurnClassification.VALID_INIT_WITH_TOOLS


def test_classify_turn_init_with_regular_tools_is_order_independent():
    msg = AIMessage(
        content="",
        tool_calls=[
            _call("read", {"file_path": "x.py"}, "call_read"),
            _call("turn_init", {"goal": "Inspect file"}, "call_init"),
        ],
    )
    assert classify_turn_call(msg) == TurnClassification.VALID_INIT_WITH_TOOLS


@pytest.mark.parametrize(
    "args",
    [
        {"goal": "Fix it", "extra": True},
        {"goal": "Fix it", "thought": "thinking"},
        {"params": {"goal": "Fix it"}},
        {"arguments": {"goal": "Fix it"}},
        {"task": "Fix it"},
        {"objective": "Fix it"},
    ],
)
def test_classify_turn_init_accepts_relaxed_args(args):
    msg = AIMessage(content="", tool_calls=[_call("turn_init", args)])
    assert classify_turn_call(msg) == TurnClassification.VALID_INIT


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"goal": ""},
        {"goal": "  "},
        {"goal": [{"type": "text", "text": "Fix it"}]},
        {"goal": None},
        {"other": 123},
    ],
)
def test_classify_turn_init_rejects_empty_or_invalid_args(args):
    msg = AIMessage(content="", tool_calls=[_call("turn_init", args)])
    assert classify_turn_call(msg) == TurnClassification.INVALID_TURN


def test_classify_legacy_turn_call_is_invalid():
    msg = AIMessage(
        content="",
        tool_calls=[_call("turn", {"operation": "start", "params": {"goal": "Fix it"}})],
    )
    assert classify_turn_call(msg) == TurnClassification.INVALID_TURN


def test_stop_operation_is_not_a_valid_turn_control_call():
    msg = AIMessage(
        content="Here is the answer.",
        tool_calls=[_call("turn", {"operation": "stop", "params": None})],
    )
    assert classify_turn_call(msg) == TurnClassification.INVALID_TURN


def test_classify_legacy_decision_and_empty_turn_calls_as_invalid():
    for args in ({"decision": "stop"}, {}):
        msg = AIMessage(content="", tool_calls=[_call("turn", args)])
        assert classify_turn_call(msg) == TurnClassification.INVALID_TURN


def test_classify_multiple_turn_init_calls_as_invalid():
    msg = AIMessage(
        content="",
        tool_calls=[
            _call("turn_init", {"goal": "one"}, "call_one"),
            _call("turn_init", {"goal": "two"}, "call_two"),
        ],
    )
    assert classify_turn_call(msg) == TurnClassification.INVALID_TURN


def test_classify_regular_tools_and_plain_text():
    assert classify_turn_call(_ai_with_regular_tool_call()) == TurnClassification.REGULAR_TOOLS
    assert classify_turn_call(_ai_plain_text()) == TurnClassification.PLAIN_TEXT
    assert classify_turn_call(AIMessage(content="")) == TurnClassification.PLAIN_TEXT


# ── Terminal normalization ───────────────────────────────────────────────────


def test_normalize_terminal_message_strips_tool_calls():
    pending = AIMessage(
        content="Final answer.",
        additional_kwargs={
            "tool_calls": [{"name": "turn_init", "args": {"goal": "x"}, "id": "call_1"}],
            "response_metadata": {"model": "gpt-4", "finish_reason": "tool_calls"},
        },
        tool_calls=[_call("turn_init", {"goal": "x"})],
    )
    terminal = normalize_terminal_message(pending)
    assert isinstance(terminal, AIMessage)
    assert terminal.content == "Final answer."
    assert terminal.tool_calls == []
    assert terminal.invalid_tool_calls == []
    assert "tool_calls" not in terminal.additional_kwargs
    assert terminal.additional_kwargs["response_metadata"]["model"] == "gpt-4"


def test_normalize_terminal_message_preserves_content():
    assert normalize_terminal_message(_ai_plain_text("Multi\nline\nanswer.")).content == (
        "Multi\nline\nanswer."
    )


# ── Control handling ─────────────────────────────────────────────────────────


def _graph():
    return SimpleNamespace(
        _turn_metrics=SimpleNamespace(increment=lambda _name: None),
        _task_state=None,
        _invalidate_tui_for_turn=lambda: None,
    )


@pytest.mark.asyncio
async def test_invalid_turn_is_repaired_twice_before_failing():
    graph = _graph()
    loop = LlmLoopState(context_tokens=0)
    runtime_task_state = TaskState()
    assistant = AIMessage(content="", tool_calls=[_call("turn_init", {}, "bad")])

    results = []
    messages = []
    for _ in range(3):
        result = await handle_turn_control_response(
            graph=graph,
            assistant_msg=assistant,
            llm_messages=messages,
            loop=loop,
            turn_state="running",
            runtime_task_state=runtime_task_state,
            state_messages=[],
            interaction_mode_value="auto",
            estimate_tokens=len,
            rerender_task_context=lambda current, _state, _task: current,
        )
        results.append(result)
        messages = result.llm_messages

    assert [result.action for result in results] == ["retry", "retry", "fail"]


@pytest.mark.asyncio
async def test_plain_text_is_normalized_as_terminal_without_stop_call():
    graph = _graph()
    loop = LlmLoopState(context_tokens=0)
    runtime_task_state = TaskState()
    result = await handle_turn_control_response(
        graph=graph,
        assistant_msg=AIMessage(
            content="Final answer.",
            tool_calls=[_call("turn", {"operation": "stop", "params": None})],
        ),
        llm_messages=[],
        loop=loop,
        turn_state="running",
        runtime_task_state=runtime_task_state,
        state_messages=[],
        interaction_mode_value="auto",
        estimate_tokens=len,
        rerender_task_context=lambda current, _state, _task: current,
    )

    assert result.action == "break"
    assert result.turn_state == "committed"
    assert loop.terminal_msg is not None
    assert loop.terminal_msg.content == "Final answer."
    assert loop.terminal_msg.tool_calls == []


# ── Prompt contracts ─────────────────────────────────────────────────────────


def test_repair_prompts_are_non_empty_and_have_no_stop_instruction():
    for prompt in (FIRST_MISS_PROMPT, SECOND_MISS_PROMPT, TURN_INIT_PROMPT, INVALID_TURN_PROMPT, LOOP_DECISION_PROMPT):
        assert len(prompt.strip()) > 10
        assert "operation='stop'" not in prompt
        assert "turn stop" not in prompt.lower()


def test_turn_init_prompt_requests_initialization_and_plain_text_completion():
    prompt = TURN_INIT_PROMPT.lower()
    assert "turn_init" in prompt
    assert "initial" in prompt


def test_invalid_turn_prompt_does_not_expose_stop_semantics():
    prompt = INVALID_TURN_PROMPT.lower()
    assert "turn_init" in prompt
    assert "plain text" not in prompt
    assert "when finished" not in prompt
    assert "stop" not in prompt
