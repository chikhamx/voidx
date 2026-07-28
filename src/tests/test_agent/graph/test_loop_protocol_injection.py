"""Loop-profile turns inject only the loop protocol tool, never the turn tool."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage

from voidx.agent.domain.loop import LOOP_PROFILE
from voidx.agent.infrastructure.langgraph.runtime.thread_context import (
    ThreadExecutionState,
    _CURRENT_THREAD_EXECUTION_STATE,
)
from tests.test_agent.graph.test_turn_control_e2e import (
    ScriptedStreamingModel,
    _make_graph,
    _text_chunk,
    _turn_chunk,
)


def _loop_commit_chunk() -> AIMessageChunk:
    return AIMessageChunk(
        content="",
        tool_calls=[{
            "name": "loop",
            "args": {"operation": "commit", "outcome": "completed", "summary": "done"},
            "id": "lc1",
            "type": "tool_call",
        }],
    )


@pytest.mark.asyncio
async def test_default_profile_injects_turn_tool(tmp_path, monkeypatch) -> None:
    model = ScriptedStreamingModel([[_text_chunk("Hi.")], [_turn_chunk()]])
    graph = _make_graph(tmp_path, model, monkeypatch)

    await graph._call_llm({
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
        "turn_state": "running",
    })

    names = [d["function"]["name"] for d in model.bound_tools]
    assert "turn" in names


@pytest.mark.asyncio
async def test_loop_profile_injects_loop_tool_and_not_turn(tmp_path, monkeypatch) -> None:
    model = ScriptedStreamingModel([[_loop_commit_chunk()]])
    graph = _make_graph(tmp_path, model, monkeypatch)
    token = _CURRENT_THREAD_EXECUTION_STATE.set(
        ThreadExecutionState(runtime_profile=LOOP_PROFILE)
    )
    try:
        await graph._call_llm({
            "messages": [HumanMessage(content="[loop] check mentions")],
            "step_count": 0,
            "persona": "coordinate",
            "turn_state": "running",
        })
    finally:
        _CURRENT_THREAD_EXECUTION_STATE.reset(token)

    names = [d["function"]["name"] for d in model.bound_tools]
    assert "loop" in names
    assert "turn" not in names
