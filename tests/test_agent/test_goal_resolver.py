import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest
from langchain_core.messages import AIMessage

from voidx.agent.goal_resolver import resolve_goal_for_turn
from voidx.agent.graph import VoidXGraph
from voidx.agent.runtime_context import TaskIntent
from voidx.agent.task_state import Goal, GoalResolution, GoalType, TaskState
from voidx.config import Config
from voidx.memory.session import (
    MessageRow,
    create_session,
    delete_session,
    load_messages,
    save_message,
)
from voidx.ui.output.dock import BottomInputDock, set_dock


class StructuredModel:
    def __init__(self, result):
        self.result = result
        self.messages = None

    def with_structured_output(self, schema):
        assert schema is GoalResolution
        return self

    async def ainvoke(self, messages):
        self.messages = messages
        return self.result


@pytest.mark.asyncio
async def test_goal_resolver_uses_structured_llm_result():
    model = StructuredModel(
        GoalResolution(
            intent=TaskIntent.CODING,
            goal=Goal(
                type=GoalType.REVIEW,
                target="src/voidx/runtime/task_state.py",
                user_requested_write=False,
            ),
            confidence=0.91,
            reason="review requested",
        )
    )

    result = await resolve_goal_for_turn(
        model=model,
        user_text="review 这个文件",
        interaction_mode="auto",
        task_state=TaskState(),
        workspace="/tmp/workspace",
        session_time="2026-06-12 CST",
    )

    assert result.intent == TaskIntent.CODING
    assert result.goal is not None
    assert result.goal.type == GoalType.REVIEW
    assert result.goal.target == "src/voidx/runtime/task_state.py"
    assert model.messages is not None
    assert "GoalResolution JSON schema" in model.messages[0].content
    assert "review 这个文件" in model.messages[1].content


@pytest.mark.asyncio
async def test_goal_resolver_plan_mode_forces_design_goal():
    model = StructuredModel(
        {
            "intent": "coding",
            "goal": {
                "type": "feature",
                "target": "实现登录",
                "expected_result": "",
                "user_requested_write": True,
                "needs_confirmation": False,
            },
            "confidence": 0.8,
            "reason": "model saw implementation words",
        }
    )

    result = await resolve_goal_for_turn(
        model=model,
        user_text="实现登录",
        interaction_mode="plan",
        task_state=TaskState(),
        workspace="/tmp/workspace",
        session_time="2026-06-12 CST",
    )

    assert result.intent == TaskIntent.CODING
    assert result.goal is not None
    assert result.goal.type == GoalType.DESIGN
    assert result.goal.user_requested_write is False
    assert result.goal.needs_confirmation is True


@pytest.mark.asyncio
async def test_goal_resolver_falls_back_when_structured_output_fails():
    class BrokenModel:
        def with_structured_output(self, _schema):
            raise RuntimeError("unsupported")

    result = await resolve_goal_for_turn(
        model=BrokenModel(),
        user_text="看看 runtime 状态",
        interaction_mode="auto",
        task_state=TaskState(),
        workspace="/tmp/workspace",
        session_time="2026-06-12 CST",
    )

    assert result.intent == TaskIntent.CODING
    assert result.goal is None


@pytest.mark.asyncio
async def test_run_once_writes_structured_goal_to_initial_task_state(tmp_path):
    """Structured goal is in initial["task_state"] before graph invoke, and resolver messages never enter history."""
    session = await create_session(workspace=str(tmp_path))
    try:
        graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None, session=session)

        expected_goal = Goal(
            type=GoalType.REVIEW,
            target="src/voidx/runtime/task_state.py",
            user_requested_write=False,
        )
        graph.model = StructuredModel(
            GoalResolution(
                intent=TaskIntent.CODING,
                goal=expected_goal,
                confidence=0.91,
                reason="review requested",
            )
        )

        captured_initial: dict = {}

        class FakeGraph:
            async def ainvoke(self, initial, _config):
                captured_initial.update(initial)
                return {"messages": list(initial["messages"]) + [AIMessage(content="ok")]}

        graph.graph = FakeGraph()

        test_dock = BottomInputDock()
        set_dock(test_dock)
        test_dock.begin_capture()
        try:
            await graph._run_once("review 这个文件")
        finally:
            test_dock.deactivate()
            test_dock.reset()
            set_dock(None)

        # Resolver goal is written to initial task_state before graph invoke.
        ts = captured_initial.get("task_state", {})
        if isinstance(ts, dict):
            assert ts.get("current_goal", {}).get("type") == "review"
            assert ts.get("current_goal", {}).get("target") == "src/voidx/runtime/task_state.py"
        else:
            goal = ts.current_goal
            assert goal is not None
            assert goal.type == GoalType.REVIEW

        # Resolver messages are not persisted to user-visible history.
        rows = await load_messages(session.id)
        for row in rows:
            assert "GoalResolution JSON schema" not in (row.content or "")
            assert "You are voidx resolving" not in (row.content or "")
    finally:
        await delete_session(session.id)
