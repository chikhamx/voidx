"""Loop turns must not end until the model submits a loop tool decision."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from voidx.agent.domain.loop import LoopSpec
from voidx.agent.infrastructure.langgraph.runtime.core.loop import LlmLoopState
from voidx.agent.infrastructure.langgraph.runtime.core.turn import handle_turn_control_response
from voidx.agent.infrastructure.langgraph.runtime.turn_control import LOOP_DECISION_PROMPT
from voidx.agent.infrastructure.langgraph.runtime.control_protocol import LoopProtocol
from voidx.agent.loop.controller import LoopAttemptController
from voidx.llm.message_markers import GUIDANCE_MARKER
from voidx.runtime.task_state import TaskState


@dataclass
class _Metrics:
    counts: dict

    def increment(self, name: str) -> None:
        self.counts[name] = self.counts.get(name, 0) + 1


class _FakeGraph:
    def __init__(self) -> None:
        self._turn_metrics = _Metrics({})
        self._task_state = TaskState()

    def _invalidate_tui_for_turn(self) -> None:
        return None


def _turn_stop_msg() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{
            "name": "turn",
            "args": {"operation": "stop", "params": None},
            "id": "call_stop",
            "type": "tool_call",
        }],
    )


def _loop_state_with_provisional() -> LlmLoopState:
    loop = LlmLoopState(context_tokens=0)
    loop.pending_provisional = AIMessage(content="TypeX MCP 不可用,需要用户排查。")
    loop.pending_provisional_visible = True
    return loop


def _loop_protocol() -> LoopProtocol:
    return LoopProtocol()


def _controller() -> LoopAttemptController:
    return LoopAttemptController(spec=LoopSpec(prompt="check mentions"))


async def _handle(graph, msg, loop, *, controller, turn_state="running", protocol=None):
    return await handle_turn_control_response(
        graph=graph,
        assistant_msg=msg,
        llm_messages=[HumanMessage(content="[loop] check mentions")],
        loop=loop,
        turn_state=turn_state,
        runtime_task_state=TaskState(),
        state_messages=[HumanMessage(content="[loop] check mentions")],
        interaction_mode_value="auto",
        estimate_tokens=lambda msgs: 0,
        rerender_task_context=lambda msgs, _state, _ts: msgs,
        loop_controller=controller,
        protocol=protocol if protocol is not None else _loop_protocol(),
    )


@pytest.mark.asyncio
async def test_loop_turn_stop_without_decision_prompts_for_loop_decision() -> None:
    graph = _FakeGraph()
    loop = _loop_state_with_provisional()

    result = await _handle(graph, _turn_stop_msg(), loop, controller=_controller())

    assert result.action == "retry"
    assert loop.protocol_repairs == 1
    last = result.llm_messages[-1]
    assert isinstance(last, HumanMessage)
    assert last.content == LOOP_DECISION_PROMPT
    assert last.additional_kwargs.get(GUIDANCE_MARKER) is True
    assert loop.terminal_msg is None


@pytest.mark.asyncio
async def test_loop_plain_text_commit_without_decision_prompts_for_loop_decision() -> None:
    graph = _FakeGraph()
    loop = LlmLoopState(context_tokens=0)

    result = await _handle(
        graph,
        AIMessage(content="TypeX MCP 不可用,需要用户排查。"),
        loop,
        controller=_controller(),
        turn_state="initial",
    )

    assert result.action == "retry"
    assert result.llm_messages[-1].content == LOOP_DECISION_PROMPT
    assert loop.terminal_msg is None


@pytest.mark.asyncio
async def test_loop_turn_stop_allowed_after_repairs_exhausted() -> None:
    graph = _FakeGraph()
    loop = _loop_state_with_provisional()
    loop.protocol_repairs = 2

    result = await _handle(graph, _turn_stop_msg(), loop, controller=_controller())

    assert result.action == "break"


@pytest.mark.asyncio
async def test_loop_turn_stop_commits_once_decision_submitted() -> None:
    graph = _FakeGraph()
    loop = _loop_state_with_provisional()
    controller = _controller()
    await controller.submit_decision(
        controller.spec_decision(outcome="continue", summary="本轮检查完成")
    )

    result = await _handle(graph, _turn_stop_msg(), loop, controller=controller)

    assert result.action == "break"
    assert result.turn_state == "committed"


@pytest.mark.asyncio
async def test_regular_turn_stop_unaffected_without_loop_controller() -> None:
    graph = _FakeGraph()
    loop = _loop_state_with_provisional()

    result = await _handle(graph, _turn_stop_msg(), loop, controller=None)

    assert result.action == "break"
    assert result.turn_state == "committed"
