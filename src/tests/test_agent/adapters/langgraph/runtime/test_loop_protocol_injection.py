"""Loop-profile turns inject only the loop protocol tool, never the turn tool."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

from voidx.agent.domain.automation.loop import LOOP_PROFILE
from voidx.agent.domain.automation.goal import GOAL_PROFILE
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.application.automation.goal.controller import GoalController
from voidx.agent.adapters.langgraph.runtime.thread_context import (
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
            "name": "goal_decision",
            "args": {
                "status": "finished",
                "summary": "done",
                "evidence": ["verified"],
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
    assert "goal_init" in names
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
    assert result["messages"][0].tool_calls[0]["name"] == "goal_decision"




@pytest.mark.asyncio
async def test_goal_evaluator_after_decision_requests_final_text_without_tools(
    tmp_path,
    monkeypatch,
) -> None:
    class RecordingModel(ScriptedStreamingModel):
        def __init__(self):
            super().__init__([[_text_chunk("验收完成，所有条件均有证据支持。")]])
            self.bind_calls = 0
            self.requests: list[list] = []

        def bind_tools(self, tool_defs, **kwargs):
            self.bind_calls += 1
            self.bound_tools = tool_defs
            return self

        async def astream(self, messages):
            self.requests.append(list(messages))
            async for chunk in super().astream(messages):
                yield chunk

    model = RecordingModel()
    graph = _make_graph(tmp_path, model, monkeypatch)
    controller = GoalController()
    await controller.submit_decision(
        {
            "outcome": "completed",
            "summary": "acceptance verified",
            "progress": "meaningful",
        },
        protocol_id="decision-1",
    )
    token = _CURRENT_THREAD_EXECUTION_STATE.set(
        ThreadExecutionState(
            runtime_profile=GOAL_PROFILE,
            turn_context=TurnExecutionContext(
                thread_id="goal:parent:active:evaluator",
                session_id="goal-evaluator-session",
                runtime_profile=GOAL_PROFILE,
                workspace=str(tmp_path),
                goal_controller=controller,
                goal_phase="evaluator",
            ),
        )
    )
    try:
        result = await graph._call_llm({
            "messages": [
                HumanMessage(content="[goal:evaluator] verify"),
                AIMessage(
                    content="",
                    tool_calls=[{
                        "name": "goal_decision",
                        "args": {
                            "status": "finished",
                            "summary": "acceptance verified",
                            "progress": "meaningful",
                        },
                        "id": "decision-1",
                        "type": "tool_call",
                    }],
                ),
                ToolMessage(
                    content="Goal decision durably recorded.",
                    tool_call_id="decision-1",
                    name="goal_decision",
                ),
            ],
            "step_count": 1,
            "persona": "review",
            "turn_state": "initial",
        })
    finally:
        _CURRENT_THREAD_EXECUTION_STATE.reset(token)

    assert model.call_index == 1
    assert model.bind_calls == 0
    assert any(
        isinstance(message, HumanMessage)
        and "decision has been durably submitted" in str(message.content)
        for message in model.requests[0]
    )
    assert result["messages"][0].content == "验收完成，所有条件均有证据支持。"
    assert result["messages"][0].tool_calls == []



@pytest.mark.asyncio
async def test_goal_evaluator_final_response_retries_tool_call_as_plain_text(
    tmp_path,
    monkeypatch,
) -> None:
    class RecordingModel(ScriptedStreamingModel):
        def __init__(self):
            super().__init__([
                [_goal_decision_chunk()],
                [_text_chunk("验收完成，目标已经闭环。")],
            ])
            self.bind_calls = 0
            self.requests: list[list] = []

        def bind_tools(self, tool_defs, **kwargs):
            self.bind_calls += 1
            self.bound_tools = tool_defs
            return self

        async def astream(self, messages):
            self.requests.append(list(messages))
            async for chunk in super().astream(messages):
                yield chunk

    model = RecordingModel()
    graph = _make_graph(tmp_path, model, monkeypatch)
    controller = GoalController()
    await controller.submit_decision(
        {
            "outcome": "completed",
            "summary": "acceptance verified",
            "progress": "meaningful",
        },
        protocol_id="decision-1",
    )
    token = _CURRENT_THREAD_EXECUTION_STATE.set(
        ThreadExecutionState(
            runtime_profile=GOAL_PROFILE,
            turn_context=TurnExecutionContext(
                thread_id="goal:parent:active:evaluator",
                session_id="goal-evaluator-session",
                runtime_profile=GOAL_PROFILE,
                workspace=str(tmp_path),
                goal_controller=controller,
                goal_phase="evaluator",
            ),
        )
    )
    try:
        result = await graph._call_llm({
            "messages": [
                HumanMessage(content="[goal:evaluator] verify"),
                AIMessage(
                    content="",
                    tool_calls=[{
                        "name": "goal_decision",
                        "args": {
                            "status": "finished",
                            "summary": "acceptance verified",
                            "progress": "meaningful",
                        },
                        "id": "decision-1",
                        "type": "tool_call",
                    }],
                ),
                ToolMessage(
                    content="Goal decision durably recorded.",
                    tool_call_id="decision-1",
                    name="goal_decision",
                ),
            ],
            "step_count": 1,
            "persona": "review",
            "turn_state": "initial",
        })
    finally:
        _CURRENT_THREAD_EXECUTION_STATE.reset(token)

    assert model.call_index == 2
    assert model.bind_calls == 0
    assert any(
        isinstance(message, HumanMessage)
        and "plain text only" in str(message.content)
        for message in model.requests[-1]
    )
    assert result["messages"][0].content == "验收完成，目标已经闭环。"
    assert result["messages"][0].tool_calls == []

@pytest.mark.asyncio
async def test_goal_evaluator_exhaustion_forces_decision_tool_and_reports_failure(
    tmp_path,
    monkeypatch,
) -> None:
    class RecordingModel(ScriptedStreamingModel):
        def __init__(self):
            super().__init__([
                [_text_chunk("Looks done.")],
                [_text_chunk("Still looks done.")],
                [_text_chunk("No decision tool call.")],
            ])
            self.bound_tool_history: list[tuple[list[str], dict]] = []

        def bind_tools(self, tool_defs, **kwargs):
            names = [item["function"]["name"] for item in tool_defs]
            self.bound_tool_history.append((names, kwargs))
            self.bound_tools = tool_defs
            return self

    model = RecordingModel()
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

    assert model.call_index == 3
    assert model.bound_tool_history[-1][0] == ["goal_decision"]
    assert result["should_continue"] is False
    assert result["stop_signal"] == "missing_goal_decision"
