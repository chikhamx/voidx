import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from voidx.agent.goal_resolver import resolve_goal_for_turn
from voidx.agent.graph import VoidXGraph
from voidx.agent.graph.turn_runner import _turn_exchange_from_final_messages
from voidx.agent.task_state import (
    GoalResolution,
    GoalSpec,
    GoalType,
    IntentResolution,
    PlanResolution,
    TaskState,
    TurnExchange,
    WorkflowRoute,
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
async def test_goal_resolver_validation_error_falls_back_to_general(tmp_path, monkeypatch):
    from voidx.logging import request_log

    monkeypatch.setattr(request_log, "_DEFAULT_LOG_DIR", tmp_path)

    class InvalidStructuredModel:
        def with_structured_output(self, _schema):
            return self

        async def ainvoke(self, _messages):
            return GoalResolution.model_validate({
                "intent": {"type": "coding", "desc": "bug fix"},
                "goal": {"type": "bug", "desc": "帮我修一个 bug"},
                "plan": {"join": "debug", "leave": "verify"},
            })

    result = await resolve_goal_for_turn(
        model=InvalidStructuredModel(),
        user_text="帮我修一个 bug",
        interaction_mode="auto",
        task_state=TaskState(),
    )

    assert result.intent.type == TaskIntent.GENERAL
    assert result.goal is None
    assert result.plan is None

    entries = [
        json.loads(line)
        for line in (tmp_path / "llm_requests.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    exchange = next(entry for entry in entries if entry.get("event") == "goal_resolver_exchange")
    assert exchange["response"]["error_type"] == "ValidationError"
    assert "bug" in exchange["response"]["error"]
    entry = next(entry for entry in entries if entry.get("event") == "goal_resolver_decision")
    assert entry["fallback_reason"] == "structured_output_error"
    assert entry["fallback_error_type"] == "ValidationError"
    assert "bug" in entry["fallback_error"]


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
                assert "GoalResolution JSON schema" not in messages[0].content
                assert messages[-1].content == "review 这个文件"
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
        assert ts.get("recent_exchanges") == []
        assert graph._task_state.recent_exchanges == [
            TurnExchange(user_text="review 这个文件", assistant_text="ok")
        ]

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
    )

    assert result.plan is None
    assert result.goal is None


@pytest.mark.asyncio
async def test_general_intent_with_active_workflow_preserves_coding():
    """GENERAL intent from LLM + active workflow → override to CODING, keep goal/join."""
    from voidx.workflow.types import WorkflowRunState

    task_state = TaskState(
        current_intent=TaskIntent.CODING,
        current_goal=GoalSpec(type=GoalType.FEATURE, desc="implement edit tool"),
        workflow_route=WorkflowRoute(join="tdd"),
        workflow_runs={"tdd": WorkflowRunState(name="tdd", status="active")},
    )
    model = StructuredModel(
        GoalResolution(
            intent=IntentResolution(type=TaskIntent.GENERAL, desc=""),
            goal=None,
            plan=None,
        )
    )

    result = await resolve_goal_for_turn(
        model=model,
        user_text="改",
        interaction_mode="auto",
        task_state=task_state,
    )

    assert result.intent.type == TaskIntent.CODING
    assert result.intent.desc == "continuation of active workflow"
    assert result.goal is not None
    assert result.goal.type == GoalType.FEATURE
    assert result.goal.desc == "implement edit tool"
    assert result.plan is not None
    assert result.plan.join == "tdd"


@pytest.mark.asyncio
async def test_general_intent_without_active_workflow_falls_back():
    """GENERAL intent + no active workflow → stays GENERAL, goal=null."""
    model = StructuredModel(
        GoalResolution(
            intent=IntentResolution(type=TaskIntent.GENERAL, desc=""),
            goal=None,
            plan=None,
        )
    )

    result = await resolve_goal_for_turn(
        model=model,
        user_text="改",
        interaction_mode="auto",
        task_state=TaskState(),
    )

    assert result.intent.type == TaskIntent.GENERAL
    assert result.goal is None
    assert result.plan is None


@pytest.mark.asyncio
async def test_general_intent_with_workflow_route_but_no_goal_falls_back():
    """GENERAL intent + active workflow route but no current_goal → falls back to GENERAL."""
    from voidx.workflow.types import WorkflowRunState

    task_state = TaskState(
        workflow_route=WorkflowRoute(join="tdd"),
        workflow_runs={"tdd": WorkflowRunState(name="tdd", status="active")},
    )
    model = StructuredModel(
        GoalResolution(
            intent=IntentResolution(type=TaskIntent.GENERAL, desc=""),
            goal=None,
            plan=None,
        )
    )

    result = await resolve_goal_for_turn(
        model=model,
        user_text="改",
        interaction_mode="auto",
        task_state=task_state,
    )

    assert result.intent.type == TaskIntent.GENERAL
    assert result.goal is None
    assert result.plan is None


@pytest.mark.asyncio
async def test_general_intent_with_active_workflow_from_runs():
    """GENERAL intent + active workflow in workflow_runs (no route) → override to CODING."""
    from voidx.workflow.types import WorkflowRunState

    task_state = TaskState(
        current_intent=TaskIntent.CODING,
        current_goal=GoalSpec(type=GoalType.BUGFIX, desc="fix resolver crash"),
        workflow_runs={"debug": WorkflowRunState(name="debug", status="active")},
    )
    model = StructuredModel(
        GoalResolution(
            intent=IntentResolution(type=TaskIntent.GENERAL, desc=""),
            goal=None,
            plan=None,
        )
    )

    result = await resolve_goal_for_turn(
        model=model,
        user_text="继续",
        interaction_mode="auto",
        task_state=task_state,
    )

    assert result.intent.type == TaskIntent.CODING
    assert result.goal is not None
    assert result.goal.type == GoalType.BUGFIX
    assert result.plan is not None
    assert result.plan.join == "debug"


@pytest.mark.asyncio
async def test_resolver_prompt_includes_active_workflow_state():
    """When current_goal is set, the system prompt includes active workflow info."""
    from voidx.workflow.types import WorkflowRunState

    task_state = TaskState(
        current_intent=TaskIntent.CODING,
        current_goal=GoalSpec(type=GoalType.FEATURE, desc="implement edit tool"),
        workflow_route=WorkflowRoute(join="tdd"),
        workflow_runs={"tdd": WorkflowRunState(name="tdd", status="active")},
    )
    model = StructuredModel(
        GoalResolution(
            intent=IntentResolution(type=TaskIntent.CODING, desc="continue"),
            goal=GoalSpec(type=GoalType.FEATURE, desc="implement edit tool"),
            plan=PlanResolution(join="tdd", leave="verify"),
        )
    )

    await resolve_goal_for_turn(
        model=model,
        user_text="改",
        interaction_mode="auto",
        task_state=task_state,
    )

    assert model.messages is not None
    system_prompt = model.messages[0].content
    assert "Current state:" in system_prompt
    assert "intent: coding" in system_prompt
    assert "goal: feature" in system_prompt
    assert "active workflows: tdd" in system_prompt


@pytest.mark.asyncio
async def test_resolver_prompt_no_current_state_when_no_goal():
    """When no current_goal, the system prompt omits current state section."""
    model = StructuredModel(
        GoalResolution(
            intent=IntentResolution(type=TaskIntent.CODING, desc="review"),
            goal=GoalSpec(type=GoalType.REVIEW, desc="review code"),
            plan=PlanResolution(join="review", leave="review"),
        )
    )

    await resolve_goal_for_turn(
        model=model,
        user_text="review 一下",
        interaction_mode="auto",
        task_state=TaskState(),
    )

    assert model.messages is not None
    system_prompt = model.messages[0].content
    assert "Current state:" not in system_prompt
    assert "active workflows" not in system_prompt


@pytest.mark.asyncio
async def test_resolver_prompt_includes_short_continuation_rule():
    """The system prompt includes the short continuation rule for active workflows."""
    model = StructuredModel(
        GoalResolution(
            intent=IntentResolution(type=TaskIntent.CODING, desc="review"),
            goal=GoalSpec(type=GoalType.REVIEW, desc="review code"),
            plan=PlanResolution(join="review", leave="review"),
        )
    )

    await resolve_goal_for_turn(
        model=model,
        user_text="review 一下",
        interaction_mode="auto",
        task_state=TaskState(),
    )

    assert model.messages is not None
    system_prompt = model.messages[0].content
    assert "short continuation" in system_prompt
    assert "active workflow" in system_prompt


@pytest.mark.asyncio
async def test_intent_window_size_4_includes_more_context():
    """With _INTENT_WINDOW_SIZE=4, the resolver sees up to 3 previous exchanges."""
    exchanges = [
        TurnExchange(user_text="实现一个 edit tool", assistant_text="好的，开始实现"),
        TurnExchange(user_text="先写测试", assistant_text="测试已写好"),
        TurnExchange(user_text="运行测试", assistant_text="测试通过了"),
    ]
    task_state = TaskState(recent_exchanges=exchanges)
    model = StructuredModel(
        GoalResolution(
            intent=IntentResolution(type=TaskIntent.CODING, desc="continue"),
            goal=GoalSpec(type=GoalType.FEATURE, desc="implement edit tool"),
            plan=PlanResolution(join="tdd", leave="verify"),
        )
    )

    await resolve_goal_for_turn(
        model=model,
        user_text="改",
        interaction_mode="auto",
        task_state=task_state,
    )

    assert model.messages is not None
    user_messages = [m for m in model.messages[1:] if isinstance(m, HumanMessage)]
    assert len(user_messages) == 4
    assert user_messages[0].content == "实现一个 edit tool"
    assert user_messages[1].content == "先写测试"
    assert user_messages[2].content == "运行测试"
    assert user_messages[3].content == "改"
