import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest
from langchain_core.messages import AIMessage

from voidx.agent.goal_resolver import resolve_goal_for_turn
from voidx.agent.graph import VoidXGraph
from voidx.agent.runtime_context import TaskIntent
from voidx.agent.task_state import Goal, GoalResolution, GoalType, PendingApproval, TaskState
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
    assert "set workflow_start=tdd and workflow_end=verify" in model.messages[0].content
    assert "next_workflow" not in model.messages[0].content
    assert "Do not choose brainstorm" in model.messages[0].content
    assert "review 这个文件" in model.messages[1].content
    assert "title_requested" not in model.messages[0].content
    assert "title_requested" not in model.messages[1].content


def test_goal_resolution_schema_excludes_approval_and_title_fields():
    properties = GoalResolution.model_json_schema()["properties"]

    assert "confirmed_approval" not in properties
    assert "title" not in properties
    assert "workflow_start" in properties
    assert "workflow_end" in properties
    assert "next_workflow" not in properties


@pytest.mark.asyncio
async def test_goal_resolver_propagates_review_only_route():
    model = StructuredModel(
        {
            "intent": "coding",
            "goal": {
                "type": "review",
                "target": "current diff",
                "expected_result": "review findings",
                "user_requested_write": False,
                "needs_confirmation": False,
            },
            "confidence": 0.94,
            "reason": "review only",
            "workflow_start": "review",
            "workflow_end": "review",
        }
    )

    result = await resolve_goal_for_turn(
        model=model,
        user_text="review 一下这个",
        interaction_mode="auto",
        task_state=TaskState(),
        workspace="/tmp/workspace",
        session_time="2026-06-14 CST",
    )

    assert result.workflow_start == "review"
    assert result.workflow_end == "review"
    assert result.goal is not None
    assert result.goal.user_requested_write is False


@pytest.mark.asyncio
async def test_goal_resolver_propagates_review_and_fix_route():
    model = StructuredModel(
        {
            "intent": "coding",
            "goal": {
                "type": "review",
                "target": "current diff",
                "expected_result": "review findings fixed and verified",
                "user_requested_write": True,
                "needs_confirmation": False,
            },
            "confidence": 0.94,
            "reason": "review then fix",
            "workflow_start": "review",
            "workflow_end": "verify",
        }
    )

    result = await resolve_goal_for_turn(
        model=model,
        user_text="review 完并修复问题",
        interaction_mode="auto",
        task_state=TaskState(),
        workspace="/tmp/workspace",
        session_time="2026-06-14 CST",
    )

    assert result.workflow_start == "review"
    assert result.workflow_end == "verify"
    assert result.goal is not None
    assert result.goal.user_requested_write is True


@pytest.mark.asyncio
async def test_goal_resolver_defaults_review_write_route_end_to_verify():
    model = StructuredModel(
        {
            "intent": "coding",
            "goal": {
                "type": "review",
                "target": "current diff",
                "expected_result": "review findings fixed",
                "user_requested_write": True,
                "needs_confirmation": False,
            },
            "confidence": 0.9,
            "reason": "review then fix",
            "workflow_start": "review",
        }
    )

    result = await resolve_goal_for_turn(
        model=model,
        user_text="review 完并修复问题",
        interaction_mode="auto",
        task_state=TaskState(),
        workspace="/tmp/workspace",
        session_time="2026-06-14 CST",
    )

    assert result.workflow_start == "review"
    assert result.workflow_end == "verify"


@pytest.mark.asyncio
async def test_goal_resolver_propagates_valid_workflow_start():
    model = StructuredModel(
        {
            "intent": "coding",
            "goal": {
                "type": "doc",
                "target": "写 workflow approval spec",
                "expected_result": "",
                "user_requested_write": True,
                "needs_confirmation": False,
            },
            "confidence": 0.92,
            "reason": "user approved design and requested spec",
            "workflow_start": "design-doc",
            "workflow_end": "design-doc",
        }
    )
    state = TaskState(
        pending_approval=PendingApproval(
            scope="workflow approval auto advance",
            source_goal_type=GoalType.DESIGN,
        )
    )

    result = await resolve_goal_for_turn(
        model=model,
        user_text="可以，先写一个 spec",
        interaction_mode="auto",
        task_state=state,
        workspace="/tmp/workspace",
        session_time="2026-06-14 CST",
    )

    assert result.workflow_start == "design-doc"
    assert result.workflow_end == "design-doc"
    assert result.goal is not None
    assert result.goal.type == GoalType.DOC


@pytest.mark.asyncio
async def test_goal_resolver_drops_unknown_workflow_route():
    model = StructuredModel(
        {
            "intent": "coding",
            "goal": None,
            "confidence": 0.7,
            "reason": "bad workflow target",
            "workflow_start": "nonexistent",
            "workflow_end": "also-missing",
        }
    )

    result = await resolve_goal_for_turn(
        model=model,
        user_text="继续",
        interaction_mode="auto",
        task_state=TaskState(),
        workspace="/tmp/workspace",
        session_time="2026-06-14 CST",
    )

    assert result.workflow_start is None
    assert result.workflow_end is None


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
async def test_run_once_uses_goal_resolver_and_keeps_resolver_messages_out_of_history(tmp_path):
    """Default run loop calls the structured resolver, and resolver messages never enter history."""
    session = await create_session(workspace=str(tmp_path))
    try:
        graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None, session=session)

        class ResolverShouldRunModel:
            called = False

            def with_structured_output(self, schema):
                assert schema is GoalResolution
                return self

            async def ainvoke(self, messages):
                self.called = True
                assert "GoalResolution JSON schema" in messages[0].content
                assert "review 这个文件" in messages[1].content
                return GoalResolution(
                    intent=TaskIntent.CODING,
                    goal=Goal(
                        type=GoalType.REVIEW,
                        target="review 这个文件",
                        user_requested_write=False,
                    ),
                    confidence=0.9,
                    reason="review request",
                )

        resolver_model = ResolverShouldRunModel()
        graph.model = resolver_model

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

        assert resolver_model.called is True
        ts = captured_initial.get("task_state", {})
        if isinstance(ts, dict):
            assert ts.get("current_intent") == "coding"
            assert ts.get("current_goal", {}).get("type") == "review"
            assert ts.get("recent_user_texts") == ["review 这个文件"]
        else:
            assert ts.current_intent == TaskIntent.CODING
            assert ts.current_goal is not None
            assert ts.current_goal.type == GoalType.REVIEW
            assert ts.recent_user_texts == ["review 这个文件"]

        # Resolver messages are not persisted to user-visible history.
        rows = await load_messages(session.id)
        for row in rows:
            assert "GoalResolution JSON schema" not in (row.content or "")
            assert "You are voidx resolving" not in (row.content or "")
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_goal_resolver_plain_approval_with_pending_approval_returns_no_workflow_route():
    model = StructuredModel(
        {
            "intent": "coding",
            "goal": {
                "type": "feature",
                "target": "workflow approval auto advance",
                "expected_result": "",
                "user_requested_write": True,
                "needs_confirmation": False,
            },
            "confidence": 0.8,
            "reason": "user approved pending design",
        }
    )
    state = TaskState(
        pending_approval=PendingApproval(
            scope="workflow approval auto advance",
            source_goal_type=GoalType.DESIGN,
        )
    )

    result = await resolve_goal_for_turn(
        model=model,
        user_text="可以",
        interaction_mode="auto",
        task_state=state,
        workspace="/tmp/workspace",
        session_time="2026-06-14 CST",
    )

    assert result.workflow_start is None
    assert result.workflow_end is None
    assert result.goal is not None
    assert result.goal.type == GoalType.FEATURE
    assert result.goal.user_requested_write is True


@pytest.mark.asyncio
async def test_goal_resolver_normal_request_returns_no_workflow_route():
    model = StructuredModel(
        GoalResolution(
            intent=TaskIntent.CODING,
            goal=Goal(
                type=GoalType.INSPECT,
                target="runtime state",
                user_requested_write=False,
            ),
            confidence=0.9,
            reason="inspection request",
        )
    )

    result = await resolve_goal_for_turn(
        model=model,
        user_text="看看 runtime 状态",
        interaction_mode="auto",
        task_state=TaskState(),
        workspace="/tmp/workspace",
        session_time="2026-06-14 CST",
    )

    assert result.workflow_start is None
    assert result.workflow_end is None
    assert result.goal is not None
    assert result.goal.type == GoalType.INSPECT
