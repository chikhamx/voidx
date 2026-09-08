"""Tests for turn_init initialization and plain-text completion flow."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from tests.test_agent.adapters.langgraph.runtime.test_turn_control_integration import (
    ScriptedStreamingModel,
    _make_graph,
    _regular_tool_chunk,
    _text_chunk,
    _turn_init_chunk,
)


@pytest.mark.asyncio
async def test_no_turn_init_call_falls_back_to_coding_none_goal(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([[_text_chunk("Done.")]])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
        "turn_state": "initial",
    })

    assert result["turn_state"] == "committed"
    assert result["task_state"]["current_goal"] is None


@pytest.mark.asyncio
async def test_regular_tools_do_not_inject_turn_init_prompt(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([[_regular_tool_chunk()]])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
        "turn_state": "initial",
    })

    assert result["messages"][0].tool_calls[0]["name"] == "read"
    assert model.call_index == 1


@pytest.mark.asyncio
async def test_duplicate_turn_init_returns_goal_already_declared(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_turn_init_chunk(goal="First goal")],
        [_turn_init_chunk(goal="Second goal")],
        [_text_chunk("Done.")],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
        "turn_state": "initial",
    })

    third_round_messages = model.received_messages[2]
    assert any(
        isinstance(message, ToolMessage)
        and message.name == "turn_init"
        and "Turn already initialized" in str(message.content)
        for message in third_round_messages
    )
    assert result["task_state"]["current_goal"] == {"desc": "First goal"}
    assert result["turn_state"] == "committed"


@pytest.mark.asyncio
async def test_update_after_turn_double_call_idempotent(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_turn_init_chunk(goal="Implement feature")],
        [_text_chunk("Done.")],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="start work")],
        "step_count": 0,
        "persona": "coordinate",
        "turn_state": "initial",
    })

    exchanges = result["task_state"]["recent_exchanges"]
    assert len(exchanges) <= 1


@pytest.mark.asyncio
async def test_replacement_messages_excludes_turn_init_messages(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_turn_init_chunk(goal="Implement feature")],
        [_text_chunk("Done.")],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="start work")],
        "step_count": 0,
        "persona": "coordinate",
        "turn_state": "initial",
    })

    messages = result["messages"]
    assert len(messages) == 1
    assert isinstance(messages[0], AIMessage)
    assert messages[0].content == "Done."
    assert not any(
        isinstance(message, ToolMessage) and message.name == "turn_init"
        for message in messages
    )
    assert not any(
        isinstance(message, AIMessage) and message.tool_calls
        for message in messages
    )
