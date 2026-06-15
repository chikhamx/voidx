import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest
from langchain_core.messages import AIMessage

from voidx.agent.goal_resolver import resolve_goal_for_turn
from voidx.agent.graph import VoidXGraph
from voidx.agent.task_state import (
    GoalResolution,
    GoalSpec,
    GoalType,
    IntentResolution,
    PlanResolution,
    TaskState,
)
from voidx.config import Config
from voidx.memory.session import create_session, delete_session, load_messages
from voidx.runtime.intent import TaskIntent
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
            intent=IntentResolution(type=TaskIntent.CODING, desc="review requested"),
            goal=GoalSpec(type=GoalType.REVIEW, desc="src/voidx/runtime/task_state.py"),
            plan=PlanResolution(join="review", leave="review"),
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

    assert result.intent.type == TaskIntent.CODING
    assert result.intent.desc == "review requested"
    assert result.goal is not None
    assert result.goal.type == GoalType.REVIEW
    assert result.goal.desc == "src/voidx/runtime/task_state.py"
    assert result.plan == PlanResolution(join="review", leave="review")
    assert model.messages is not None
    assert "GoalResolution JSON schema" in model.messages[0].content
    assert "Available join values" in model.messages[0].content
    assert "workflow_start" not in model.messages[0].content
    assert "next_workflow" not in model.messages[0].content
    assert "Do not choose brainstorm" in model.messages[0].content
    assert "review 这个文件" in model.messages[1].content
    assert "title_requested" not in model.messages[0].content
    assert "title_requested" not in model.messages[1].content


def test_goal_resolution_schema_excludes_removed_fields():
    properties = GoalResolution.model_json_schema()["properties"]

    assert set(properties) == {"intent", "goal", "plan"}
    assert "confirmed_approval" not in properties
    assert "title" not in properties
    assert "workflow_start" not in properties
    assert "workflow_end" not in properties
    assert "next_workflow" not in properties


@pytest.mark.asyncio
async def test_goal_resolver_propagates_review_only_route():
    model = StructuredModel(
        {
            "intent": {"type": "coding", "desc": "review only"},
            "goal": {"type": "review", "desc": "current diff"},
            "plan": {"join": "review", "leave": "review"},
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

    assert result.plan == PlanResolution(join="review", leave="review")
    assert result.goal is not None
    assert result.goal.desc == "current diff"


@pytest.mark.asyncio
async def test_goal_resolver_defaults_review_route_leave_to_join():
    model = StructuredModel(
        {
            "intent": {"type": "coding", "desc": "review only"},
            "goal": {"type": "review", "desc": "current diff"},
            "plan": {"join": "review"},
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

    assert result.plan == PlanResolution(join="review", leave="review")


@pytest.mark.asyncio
async def test_goal_resolver_defaults_write_route_leave_to_verify():
    model = StructuredModel(
        {
            "intent": {"type": "coding", "desc": "implement spec"},
            "goal": {"type": "feature", "desc": "implement current spec"},
            "plan": {"join": "tdd"},
        }
    )

    result = await resolve_goal_for_turn(
        model=model,
        user_text="按这个 spec 实现",
        interaction_mode="auto",
        task_state=TaskState(),
        workspace="/tmp/workspace",
        session_time="2026-06-14 CST",
    )

    assert result.plan == PlanResolution(join="tdd", leave="verify")


@pytest.mark.asyncio
async def test_goal_resolver_propagates_valid_plan_join():
    model = StructuredModel(
        {
            "intent": {"type": "coding", "desc": "user requested spec"},
            "goal": {"type": "doc", "desc": "write workflow approval spec"},
            "plan": {"join": "design-doc", "leave": "design-doc"},
        }
    )

    result = await resolve_goal_for_turn(
        model=model,
        user_text="可以，先写一个 spec",
        interaction_mode="auto",
        task_state=TaskState(),
        workspace="/tmp/workspace",
        session_time="2026-06-14 CST",
    )

    assert result.plan == PlanResolution(join="design-doc", leave="design-doc")
    assert result.goal is not None
    assert result.goal.type == GoalType.DOC


@pytest.mark.asyncio
async def test_goal_resolver_drops_unknown_plan_route():
    model = StructuredModel(
        {
            "intent": {"type": "coding", "desc": "bad workflow target"},
            "goal": {"type": "feature", "desc": "continue"},
            "plan": {"join": "nonexistent", "leave": "also-missing"},
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

    assert result.plan == PlanResolution(join="brainstorm", leave="verify")


@pytest.mark.asyncio
async def test_goal_resolver_drops_non_entry_plan_join():
    model = StructuredModel(
        {
            "intent": {"type": "coding", "desc": "bad workflow entry"},
            "goal": {"type": "feature", "desc": "continue"},
            "plan": {"join": "verify", "leave": "verify"},
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

    assert result.plan == PlanResolution(join="brainstorm", leave="verify")


@pytest.mark.asyncio
async def test_goal_resolver_plan_mode_forces_design_goal():
    model = StructuredModel(
        {
            "intent": {"type": "coding", "desc": "model saw implementation words"},
            "goal": {"type": "feature", "desc": "implement login"},
            "plan": {"join": "tdd", "leave": "verify"},
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

    assert result.intent.type == TaskIntent.CODING
    assert result.goal == GoalSpec(type=GoalType.DESIGN, desc="implement login")
    assert result.plan == PlanResolution(join="brainstorm", leave="verify")


@pytest.mark.asyncio
async def test_goal_resolver_goal_mode_keeps_current_goal():
    current_goal = GoalSpec(type=GoalType.CHORE, desc="clean up runtime state")
    model = StructuredModel(
        {
            "intent": {"type": "general", "desc": "model was unsure"},
            "goal": None,
            "plan": None,
        }
    )

    result = await resolve_goal_for_turn(
        model=model,
        user_text="继续",
        interaction_mode="goal",
        task_state=TaskState(current_goal=current_goal),
        workspace="/tmp/workspace",
        session_time="2026-06-12 CST",
    )

    assert result.intent.type == TaskIntent.CODING
    assert result.goal == current_goal
    assert result.plan == PlanResolution(join="tdd", leave="verify")


@pytest.mark.asyncio
async def test_goal_resolver_falls_back_to_general_when_structured_output_fails():
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

    assert result.intent.type == TaskIntent.GENERAL
    assert result.goal is None
    assert result.plan is None


@pytest.mark.asyncio
async def test_run_once_uses_goal_resolver_and_keeps_resolver_messages_out_of_history(tmp_path):
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
                    intent=IntentResolution(type=TaskIntent.CODING, desc="review request"),
                    goal=GoalSpec(type=GoalType.REVIEW, desc="review 这个文件"),
                    plan=PlanResolution(join="review", leave="review"),
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
        assert ts.get("current_intent") == "coding"
        assert ts.get("current_goal", {}).get("type") == "review"
        assert ts.get("workflow_route") == {"join": "review", "leave": "review"}
        assert ts.get("recent_user_texts") == ["review 这个文件"]

        rows = await load_messages(session.id)
        for row in rows:
            assert "GoalResolution JSON schema" not in (row.content or "")
            assert "You are voidx resolving" not in (row.content or "")
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_goal_resolver_normal_request_returns_no_workflow_route():
    model = StructuredModel(
        GoalResolution(
            intent=IntentResolution(type=TaskIntent.CODING, desc="inspection request"),
            goal=GoalSpec(type=GoalType.INSPECT, desc="runtime state"),
            plan=None,
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

    assert result.plan == PlanResolution(join="", leave=None)
    assert result.goal is not None
    assert result.goal.type == GoalType.INSPECT
