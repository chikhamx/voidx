"""Loop-profile turns inject only the loop protocol tool, never the turn tool."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

from voidx.agent.domain.automation.loop import LOOP_PROFILE
from voidx.agent.domain.automation.goal import GOAL_PROFILE
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.application.automation.goal.controller import GoalController
from voidx.agent.infrastructure.langgraph.runtime.thread_context import (
    ThreadExecutionState,
    _CURRENT_THREAD_EXECUTION_STATE,
)
from tests.test_agent.adapters.langgraph.runtime.test_turn_control_e2e import (
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


def _goal_decision_chunk() -> AIMessageChunk:
    return AIMessageChunk(
        content="",
        tool_calls=[{
            "name": "goal",
            "args": {
                "op": "decision",
                "status": "finished",
                "summary": "done",
                "objective": "",
                "acceptance_condition": "",
                "achievement_method": "",
                "max_attempts": 20,
                "evidence": "verified",
                "next": "",
                "reason": "verified",
                "progress": "meaningful",
            },
            "id": "gc1",
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
        ThreadExecutionState(
            runtime_profile=LOOP_PROFILE,
            turn_context=TurnExecutionContext(
                thread_id="loop:t",
                session_id="loop:t",
                runtime_profile=LOOP_PROFILE,
                workspace=str(tmp_path),
                loop_phase="work",
            ),
        )
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


@pytest.mark.asyncio
async def test_goal_profile_injects_goal_tool_and_not_turn(tmp_path, monkeypatch) -> None:
    model = ScriptedStreamingModel([[_text_chunk("Done.")]])
    graph = _make_graph(tmp_path, model, monkeypatch)
    token = _CURRENT_THREAD_EXECUTION_STATE.set(
        ThreadExecutionState(
            runtime_profile=GOAL_PROFILE,
            turn_context=TurnExecutionContext(
                thread_id="goal:t",
                session_id="goal:t",
                runtime_profile=GOAL_PROFILE,
                workspace=str(tmp_path),
                goal_phase="intake",
            ),
        )
    )
    try:
        result = await graph._call_llm({
            "messages": [
                HumanMessage(content="[goal] do work"),
                AIMessage(
                    content="",
                    tool_calls=[{
                        "name": "read",
                        "args": {"file_path": "x.py"},
                        "id": "tc-prior",
                        "type": "tool_call",
                    }],
                ),
                ToolMessage(content="ok", tool_call_id="tc-prior", name="read"),
            ],
            "step_count": 0,
            "persona": "coordinate",
            "turn_state": "initial",
        })
    finally:
        _CURRENT_THREAD_EXECUTION_STATE.reset(token)

    names = [d["function"]["name"] for d in model.bound_tools]
    assert "goal" in names
    assert "turn" not in names
    assert model.call_index == 1
    assert result["turn_state"] == "committed"
    assert result["messages"][0].content == "Done."


@pytest.mark.asyncio
async def test_goal_profile_uses_goal_controller_for_missing_decision_repair(tmp_path, monkeypatch) -> None:
    model = ScriptedStreamingModel([[_text_chunk("Looks done.")], [_goal_decision_chunk()]])
    graph = _make_graph(tmp_path, model, monkeypatch)
    controller = GoalController()
    token = _CURRENT_THREAD_EXECUTION_STATE.set(
        ThreadExecutionState(
            runtime_profile=GOAL_PROFILE,
            turn_context=TurnExecutionContext(
                thread_id="goal:parent:active",
                session_id="goal:parent:active",
                runtime_profile=GOAL_PROFILE,
                workspace=str(tmp_path),
                goal_controller=controller,
                goal_phase="evaluator",
            ),
        )
    )
    try:
        result = await graph._call_llm({
            "messages": [HumanMessage(content="[goal:evaluator] verify")],
            "step_count": 0,
            "persona": "coordinate",
            "turn_state": "initial",
        })
    finally:
        _CURRENT_THREAD_EXECUTION_STATE.reset(token)

    assert model.call_index == 2
    assert "should_continue" not in result
    assert result["messages"][0].tool_calls[0]["name"] == "goal"
