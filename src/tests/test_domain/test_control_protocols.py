"""Control protocol behavior: tool injection, classification, and loop decision barrier."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from voidx.agent.domain.automation.goal import GOAL_PROFILE
from voidx.agent.domain.automation.loop import LOOP_PROFILE, LoopSpec
from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.infrastructure.langgraph.runtime.core.loop import LlmLoopState
from voidx.agent.infrastructure.langgraph.runtime.control_protocol import (
    GoalProtocol,
    LoopProtocol,
    TurnToolProtocol,
)
from voidx.agent.infrastructure.langgraph.runtime.turn_control import (
    LOOP_DECISION_PROMPT,
    TurnClassification,
)
from voidx.agent.application.automation.goal.controller import GoalController
from voidx.agent.application.automation.goal.intake_controller import GoalIntakeController
from voidx.agent.application.automation.loop.controller import LoopAttemptController


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


def _loop_commit_msg() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{
            "name": "loop",
            "args": {"operation": "commit", "outcome": "continue", "summary": "done"},
            "id": "call_loop",
            "type": "tool_call",
        }],
    )


# ── TurnToolProtocol ─────────────────────────────────────────────────────────


def test_turn_protocol_injects_turn_tool_only() -> None:
    defs = TurnToolProtocol().tool_definitions()

    assert [d["function"]["name"] for d in defs] == ["turn"]


def test_turn_protocol_classifies_like_turn_control() -> None:
    protocol = TurnToolProtocol()

    assert protocol.classify(_turn_stop_msg()) == TurnClassification.VALID_TURN
    assert protocol.classify(AIMessage(content="plain")) == TurnClassification.PLAIN_TEXT
    assert protocol.classify(_loop_commit_msg()) == TurnClassification.REGULAR_TOOLS


def test_turn_protocol_never_blocks_turn_stop() -> None:
    loop = LlmLoopState(context_tokens=0)

    assert TurnToolProtocol().decision_missing(_turn_stop_msg(), loop, controller=None) is False


# ── LoopProtocol ─────────────────────────────────────────────────────────────


def test_loop_protocol_injects_loop_tool_only() -> None:
    defs = LoopProtocol().tool_definitions()

    names = [d["function"]["name"] for d in defs]
    assert names == ["loop"]
    schema = defs[0]["function"]["parameters"]
    assert "operation" in schema.get("properties", {})


def test_loop_protocol_classifies_loop_commit_as_regular_tool() -> None:
    assert LoopProtocol().classify(_loop_commit_msg()) == TurnClassification.REGULAR_TOOLS


def test_loop_protocol_blocks_turn_stop_until_decision_submitted() -> None:
    controller = LoopAttemptController(spec=LoopSpec(prompt="check"))
    loop = LlmLoopState(context_tokens=0)
    protocol = LoopProtocol()

    assert protocol.decision_missing(_turn_stop_msg(), loop, controller=controller) is True

    assert protocol.repair_prompt() == LOOP_DECISION_PROMPT


@pytest.mark.asyncio
async def test_loop_protocol_allows_turn_stop_after_decision() -> None:
    controller = LoopAttemptController(spec=LoopSpec(prompt="check"))
    loop = LlmLoopState(context_tokens=0)

    await controller.submit_decision(
        controller.spec_decision(outcome="continue", summary="done")
    )

    assert LoopProtocol().decision_missing(_turn_stop_msg(), loop, controller=controller) is False


def test_loop_protocol_stops_repairing_after_max_repairs() -> None:
    controller = LoopAttemptController(spec=LoopSpec(prompt="check"))
    loop = LlmLoopState(context_tokens=0)
    loop.protocol_repairs = 2

    assert LoopProtocol().decision_missing(_turn_stop_msg(), loop, controller=controller) is False


def test_loop_protocol_blocks_plain_text_commit_without_decision() -> None:
    controller = LoopAttemptController(spec=LoopSpec(prompt="check"))
    loop = LlmLoopState(context_tokens=0)

    assert LoopProtocol().decision_missing(AIMessage(content="回答完毕"), loop, controller=controller) is True


def test_loop_protocol_ignores_regular_tool_calls() -> None:
    controller = LoopAttemptController(spec=LoopSpec(prompt="check"))
    loop = LlmLoopState(context_tokens=0)
    msg = AIMessage(
        content="",
        tool_calls=[{"name": "read", "args": {"file_path": "x"}, "id": "c1", "type": "tool_call"}],
    )

    assert LoopProtocol().decision_missing(msg, loop, controller=controller) is False


# ── GoalProtocol ─────────────────────────────────────────────────────────────


def test_goal_protocol_exposes_goal_control_tool() -> None:
    defs = GoalProtocol().tool_definitions()

    assert [d["function"]["name"] for d in defs] == ["goal"]


# ── Controller routing ───────────────────────────────────────────────────────


class _Controller:
    def final_decision(self):
        return None


def test_turn_protocol_has_no_lifecycle_controller() -> None:
    ctx = TurnExecutionContext(thread_id="t1", session_id="s1")

    assert TurnToolProtocol().controller(ctx) is None


def test_loop_protocol_resolves_loop_controller() -> None:
    controller = _Controller()
    ctx = TurnExecutionContext(
        thread_id="t1",
        session_id="s1",
        runtime_profile=LOOP_PROFILE,
        loop_controller=controller,
    )

    assert LoopProtocol().controller(ctx) is controller


def test_goal_protocol_resolves_intake_controller_by_phase() -> None:
    intake_controller = _Controller()
    evaluator_controller = _Controller()
    ctx = TurnExecutionContext(
        thread_id="t1",
        session_id="s1",
        runtime_profile=GOAL_PROFILE,
        goal_intake_controller=intake_controller,
        goal_controller=evaluator_controller,
        goal_phase="intake",
    )

    assert GoalProtocol().controller(ctx) is intake_controller


def test_goal_protocol_resolves_evaluator_controller_by_phase() -> None:
    intake_controller = _Controller()
    evaluator_controller = _Controller()
    ctx = TurnExecutionContext(
        thread_id="t1",
        session_id="s1",
        runtime_profile=GOAL_PROFILE,
        goal_intake_controller=intake_controller,
        goal_controller=evaluator_controller,
        goal_phase="evaluator",
    )

    assert GoalProtocol().controller(ctx) is evaluator_controller


def test_goal_protocol_ignores_work_phase_controller() -> None:
    ctx = TurnExecutionContext(
        thread_id="t1",
        session_id="s1",
        runtime_profile=GOAL_PROFILE,
        goal_controller=_Controller(),
        goal_phase="work",
    )

    assert GoalProtocol().controller(ctx) is None


def test_goal_protocol_blocks_intake_until_init_submitted() -> None:
    controller = GoalIntakeController()
    loop = LlmLoopState(context_tokens=0)
    protocol = GoalProtocol(phase="intake")

    assert protocol.decision_missing(_turn_stop_msg(), loop, controller=controller) is True
    assert 'op="init"' in protocol.repair_prompt()
    assert 'op="decision"' not in protocol.repair_prompt()


def test_goal_protocol_blocks_evaluator_until_decision_submitted() -> None:
    controller = GoalController()
    loop = LlmLoopState(context_tokens=0)
    protocol = GoalProtocol(phase="evaluator")

    assert protocol.decision_missing(_turn_stop_msg(), loop, controller=controller) is True
    assert 'op="decision"' in protocol.repair_prompt()


def test_resolve_control_protocol_falls_back_to_turn_for_unknown_profile() -> None:
    from voidx.agent.infrastructure.langgraph.runtime.control_protocol import (
        resolve_control_protocol,
    )

    profile = RuntimeProfile(profile_id="custom", revision=1, name="Custom", protocol="missing")

    assert type(resolve_control_protocol(profile)) is TurnToolProtocol


# ── Loop decision terminates the iteration ────────────────────────────────────


def _bind_thread_state(loop_controller=None):
    from voidx.agent.infrastructure.langgraph.runtime.thread_context import (
        ThreadExecutionState,
        _CURRENT_THREAD_EXECUTION_STATE,
    )

    state = ThreadExecutionState()
    if loop_controller is not None:
        from voidx.agent.domain.automation.loop import LOOP_PROFILE
        from voidx.agent.domain.turn_context import TurnExecutionContext

        state.turn_context = TurnExecutionContext(
            thread_id="t1",
            session_id="s1",
            runtime_profile=LOOP_PROFILE,
            workspace="/tmp",
            loop_controller=loop_controller,
        )
    return _CURRENT_THREAD_EXECUTION_STATE.set(state)


@pytest.mark.asyncio
async def test_loop_decision_submitted_true_after_commit() -> None:
    from voidx.agent.infrastructure.langgraph.runtime.control_protocol import (
        loop_decision_submitted,
    )
    from voidx.agent.infrastructure.langgraph.runtime.thread_context import (
        _CURRENT_THREAD_EXECUTION_STATE,
    )

    controller = LoopAttemptController(spec=LoopSpec(prompt="check"))
    token = _bind_thread_state(controller)
    try:
        assert loop_decision_submitted() is False
        await controller.submit_decision(
            controller.spec_decision(outcome="continue", summary="done", next_delay_seconds=1800)
        )
        assert loop_decision_submitted() is True
    finally:
        _CURRENT_THREAD_EXECUTION_STATE.reset(token)


def test_loop_decision_submitted_false_without_context_or_controller() -> None:
    from voidx.agent.infrastructure.langgraph.runtime.control_protocol import (
        loop_decision_submitted,
    )
    from voidx.agent.infrastructure.langgraph.runtime.thread_context import (
        _CURRENT_THREAD_EXECUTION_STATE,
    )

    assert loop_decision_submitted() is False
    token = _bind_thread_state(loop_controller=None)
    try:
        assert loop_decision_submitted() is False
    finally:
        _CURRENT_THREAD_EXECUTION_STATE.reset(token)


@pytest.mark.asyncio
async def test_strip_tool_calls_after_loop_commit() -> None:
    from voidx.agent.infrastructure.langgraph.runtime.control_protocol import (
        strip_tool_calls_after_loop_commit,
    )
    from voidx.agent.infrastructure.langgraph.runtime.thread_context import (
        _CURRENT_THREAD_EXECUTION_STATE,
    )

    controller = LoopAttemptController(spec=LoopSpec(prompt="check"))
    token = _bind_thread_state(controller)
    try:
        msg = AIMessage(
            content="仍无新增提及,继续 30 分钟间隔。",
            tool_calls=[{
                "name": "mcp",
                "args": {"op": "call"},
                "id": "call_more",
                "type": "tool_call",
            }],
        )
        # Before commit: untouched.
        assert strip_tool_calls_after_loop_commit(msg).tool_calls
        await controller.submit_decision(
            controller.spec_decision(outcome="continue", summary="done", next_delay_seconds=1800)
        )
        # After commit: tool calls stripped, summary text preserved.
        stripped = strip_tool_calls_after_loop_commit(msg)
        assert stripped.tool_calls == []
        assert "30 分钟" in stripped.content
        # Plain message without tool calls passes through unchanged.
        plain = AIMessage(content="done")
        assert strip_tool_calls_after_loop_commit(plain) is plain
    finally:
        _CURRENT_THREAD_EXECUTION_STATE.reset(token)


def test_loop_protocol_blocks_regular_tools_until_decision_submitted() -> None:
    controller = LoopAttemptController(spec=LoopSpec(prompt="check"))
    loop = LlmLoopState(context_tokens=0)
    protocol = LoopProtocol()

    mixed = AIMessage(
        content="仍无新增提及,继续 30 分钟间隔。",
        tool_calls=[{
            "name": "mcp",
            "args": {"op": "call", "server": "typex", "tool": "typex.list_mentions"},
            "id": "call_mcp",
            "type": "tool_call",
        }],
    )
    assert protocol.decision_missing(mixed, loop, controller=controller) is True


@pytest.mark.asyncio
async def test_loop_protocol_allows_regular_tools_after_decision() -> None:
    controller = LoopAttemptController(spec=LoopSpec(prompt="check"))
    loop = LlmLoopState(context_tokens=0)
    protocol = LoopProtocol()

    await controller.submit_decision(
        controller.spec_decision(outcome="continue", summary="done")
    )
    mixed = AIMessage(
        content="继续 30 分钟间隔。",
        tool_calls=[{
            "name": "mcp",
            "args": {"op": "call"},
            "id": "call_mcp",
            "type": "tool_call",
        }],
    )
    assert protocol.decision_missing(mixed, loop, controller=controller) is False
