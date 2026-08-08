"""Tests for turn(start) full flow: spec Task 7.1-7.9.

Scenarios already covered in test_turn_control_integration.py:
- 7.1 start declares goal -> running
- 7.3 START_PROMPT injected once
- 7.6 stop -> committed
- 7.7 context re-render with Turn state: running

This file covers the remaining scenarios:
- 7.2 no start -> fallback coding + none goal
- 7.4 REGULAR_TOOLS does not inject START_PROMPT
- 7.5 duplicate start -> "Goal already declared."
- 7.8 update_after_turn double-call idempotency
- 7.9 replacement_messages excludes start AIMessage/ToolMessage
"""

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

from voidx.agent.adapters.langgraph.execution import LangGraphExecution
from voidx.config import Config
from voidx.llm.domain.model import ModelConfig
from tests.test_agent.adapters.langgraph.runtime.stream_llm_helpers import FakeRenderer
from tests.test_agent.adapters.langgraph.runtime.test_turn_control_integration import (
    ScriptedStreamingModel,
    _turn_args,
    _turn_start_chunk,
    _turn_call_chunk,
    _text_chunk,
    _regular_tool_chunk,
    _make_graph,
)


# ── 7.2: no start -> fallback coding + none goal ────────────────────────────


@pytest.mark.asyncio
async def test_no_start_call_falls_back_to_coding_none_goal(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_text_chunk("Done.")],
        [_turn_call_chunk()],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
        "turn_state": "initial",
    })

    assert result["turn_state"] == "committed"
    assert result["task_state"]["current_intent"] == "coding"
    assert result["task_state"]["current_goal"] is None


# ── 7.4: REGULAR_TOOLS does not inject START_PROMPT ──────────────────────────


@pytest.mark.asyncio
async def test_regular_tools_does_not_inject_start_prompt(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_regular_tool_chunk()],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
        "turn_state": "initial",
    })

    assert result["messages"][0].tool_calls[0]["name"] == "read"
    assert model.call_index == 1


# ── 7.5: duplicate start -> "Goal already declared." ────────────────────────


@pytest.mark.asyncio
async def test_duplicate_start_returns_goal_already_declared(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_turn_start_chunk(goal="First goal")],
        [_turn_start_chunk(goal="Second goal")],
        [_text_chunk("Done.")],
        [_turn_call_chunk()],
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
        getattr(msg, "name", "") == "turn" and "Goal already declared" in str(msg.content)
        for msg in third_round_messages
    )
    assert result["task_state"]["current_goal"] == {"desc": "First goal"}
    assert result["turn_state"] == "committed"


# ── 7.8: update_after_turn double-call idempotency ──────────────────────────


@pytest.mark.asyncio
async def test_update_after_turn_double_call_idempotent(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_turn_start_chunk(goal="Implement feature")],
        [_text_chunk("Done.")],
        [_turn_call_chunk()],
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


# ── 7.9: replacement_messages excludes start AIMessage/ToolMessage ──────────


@pytest.mark.asyncio
async def test_replacement_messages_excludes_start_messages(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_turn_start_chunk(goal="Implement feature")],
        [_text_chunk("Done.")],
        [_turn_call_chunk()],
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
    assert messages[0].content == "Done."
    assert not any(
        isinstance(msg, ToolMessage) and getattr(msg, "name", "") == "turn"
        for msg in messages
    )
    assert not any(
        isinstance(msg, AIMessage) and msg.tool_calls
        for msg in messages
    )
