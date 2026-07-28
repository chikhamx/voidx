from voidx.agent.domain.turn_context import TurnExecutionContext
import json
import sys
from pathlib import Path


import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from voidx.agent.application.goal_resolver import ResolverGoal, resolve_goal_for_turn
from voidx.agent.infrastructure.langgraph.execution import LangGraphExecution
from voidx.agent.infrastructure.langgraph.runtime.turn_runner import _turn_exchange_from_final_messages
from voidx.runtime.task_state import (
    GoalResolution,
    GoalSpec,
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
        assert schema is ResolverGoal
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
            return {
                "intent": "coding",
                "goal": "帮我修一个 bug",
                "workflow": "unknown",
                "kind_hint": "bug",
            }

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
    assert exchange["response"]["raw"]["workflow"] == "unknown"
    entry = next(entry for entry in entries if entry.get("event") == "goal_resolver_decision")
    assert entry["fallback_reason"] == "invalid_structured_output"
    assert entry["fallback_error_type"] == ""
    assert entry["fallback_error"] == ""


@pytest.mark.asyncio
async def test_run_turn_auto_mode_skips_goal_resolver_and_initializes_turn_state(tmp_path):
    session = await create_session(workspace=str(tmp_path))
    try:
        graph = LangGraphExecution(Config(workspace=str(tmp_path)), api_key="test-key", session=session)

        class ResolverShouldNotRunModel:
            called = False

            def with_structured_output(self, *_args, **_kwargs):
                self.called = True
                return self

            async def ainvoke(self, _messages):
                self.called = True
                raise AssertionError("auto mode should not call resolve_goal_for_turn")

        resolver_model = ResolverShouldNotRunModel()
        graph.model = resolver_model

        captured_initial: dict = {}

        class FakeGraph:
            async def astream(self, initial, _config, *, stream_mode="values"):
                captured_initial.update(initial)
                yield {"messages": list(initial["messages"]) + [AIMessage(content="ok")], "task_state": initial["task_state"]}

        graph.graph = FakeGraph()

        test_dock = BottomInputDock()
        set_dock(test_dock)
        test_dock.begin_capture()
        try:
            await graph.run_turn("review 这个文件", context=TurnExecutionContext(thread_id=getattr(graph, "session_id", "") or "coding", session_id=getattr(graph, "session_id", "") or ""))
        finally:
            test_dock.deactivate()
            test_dock.reset()
            set_dock(None)

        assert resolver_model.called is False
        assert captured_initial.get("turn_state") == "initial"
        ts = captured_initial.get("task_state", {})
        assert ts.get("current_intent") == "coding"
        assert ts.get("current_goal") is None
        assert ts.get("workflow_route") is None
        assert ts.get("recent_exchanges") == []
        assert graph._task_state.recent_exchanges == [
            TurnExchange(user_text="review 这个文件", assistant_text="ok")
        ]
        assert graph._usage_stats.total_calls == 0

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
            intent=IntentResolution(type=TaskIntent.CODING),
            goal=GoalSpec(desc="runtime state"),
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
    assert result.goal is not None


@pytest.mark.asyncio
async def test_general_intent_with_active_workflow_preserves_coding():
    """GENERAL intent from LLM + active workflow → override to CODING, keep goal/join."""
    from voidx.workflow.types import WorkflowRunState

    task_state = TaskState(
        current_intent=TaskIntent.CODING,
        current_goal=GoalSpec(desc="implement edit tool"),
        workflow_route=WorkflowRoute(join="tdd"),
        workflow_runs={"tdd": WorkflowRunState(name="tdd", status="active")},
    )
    model = StructuredModel(
        GoalResolution(
            intent=IntentResolution(type=TaskIntent.GENERAL),
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
    assert result.goal is not None
    assert result.goal.desc == "implement edit tool"
    assert result.plan is not None
    assert result.plan.join == "tdd"


@pytest.mark.asyncio
async def test_general_intent_without_active_workflow_falls_back():
    """GENERAL intent + no active workflow + no current_goal → stays GENERAL, goal is None."""
    model = StructuredModel(
        GoalResolution(
            intent=IntentResolution(type=TaskIntent.GENERAL),
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
            intent=IntentResolution(type=TaskIntent.GENERAL),
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
        current_goal=GoalSpec(desc="fix resolver crash"),
        workflow_runs={"debug": WorkflowRunState(name="debug", status="active")},
    )
    model = StructuredModel(
        GoalResolution(
            intent=IntentResolution(type=TaskIntent.GENERAL),
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
    assert result.goal.desc == "fix resolver crash"
    assert result.plan is not None
    assert result.plan.join == "debug"


@pytest.mark.asyncio
async def test_resolver_prompt_includes_active_workflow_state():
    """When current_goal is set, the system prompt includes active workflow info."""
    from voidx.workflow.types import WorkflowRunState

    task_state = TaskState(
        current_intent=TaskIntent.CODING,
        current_goal=GoalSpec(desc="implement edit tool"),
        workflow_route=WorkflowRoute(join="tdd"),
        workflow_runs={"tdd": WorkflowRunState(name="tdd", status="active")},
    )
    model = StructuredModel(
        GoalResolution(
            intent=IntentResolution(type=TaskIntent.CODING),
            goal=GoalSpec(desc="implement edit tool"),
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
    request = model.messages[1].content
    assert "# Context" in request
    assert "intent: coding" in request
    assert "goal: implement edit tool" in request
    assert "active workflows: tdd" in request


@pytest.mark.asyncio
async def test_resolver_prompt_no_current_state_when_no_goal():
    """When no current_goal, the system prompt omits current state section."""
    model = StructuredModel(
        GoalResolution(
            intent=IntentResolution(type=TaskIntent.CODING),
            goal=GoalSpec(desc="review code"),
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
    request = model.messages[1].content
    assert "# Context" in request
    assert "goal: none" in request
    assert "active workflows: none" in request


@pytest.mark.asyncio
async def test_resolver_prompt_omits_short_continuation_rule():
    """Short continuation handling is runtime fallback, not a prompt rule."""
    model = StructuredModel(
        GoalResolution(
            intent=IntentResolution(type=TaskIntent.CODING),
            goal=GoalSpec(desc="review code"),
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
    request = model.messages[1].content
    assert "short continuation" not in request
    assert "## Return Fields" not in request


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
            intent=IntentResolution(type=TaskIntent.CODING),
            goal=GoalSpec(desc="implement edit tool"),
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
    assert len(user_messages) == 1
    content = user_messages[0].content
    assert "实现一个 edit tool" in content
    assert "先写测试" in content
    assert "运行测试" in content
    assert "改" in content
    assert "## ResolverGoal Schema" not in content


@pytest.mark.asyncio
async def test_resolver_success_with_new_goal_preserves_new_goal_over_old():
    """Resolver 成功返回新 goal + 活跃 workflow → 信任新 goal，不用老 goal 覆盖。"""
    from voidx.workflow.types import WorkflowRunState

    task_state = TaskState(
        current_intent=TaskIntent.CODING,
        current_goal=GoalSpec(desc="implement edit tool"),
        workflow_route=WorkflowRoute(join="tdd"),
        workflow_runs={"tdd": WorkflowRunState(name="tdd", status="active")},
    )
    model = StructuredModel(
        GoalResolution(
            intent=IntentResolution(type=TaskIntent.GENERAL),
            goal=GoalSpec(desc="fix memory leak in resolver"),
            plan=None,
        )
    )

    result = await resolve_goal_for_turn(
        model=model,
        user_text="先修复内存泄漏",
        interaction_mode="auto",
        task_state=task_state,
    )

    assert result.goal is not None
    assert result.goal.desc == "fix memory leak in resolver"


@pytest.mark.asyncio
async def test_resolver_success_coding_with_new_goal_short_continuation_preserves_new_goal():
    """Resolver 成功返回 CODING + 新 goal + 短续接 + 活跃 workflow → 信任新 goal。"""
    from voidx.workflow.types import WorkflowRunState

    task_state = TaskState(
        current_intent=TaskIntent.CODING,
        current_goal=GoalSpec(desc="implement edit tool"),
        workflow_route=WorkflowRoute(join="tdd"),
        workflow_runs={"tdd": WorkflowRunState(name="tdd", status="active")},
    )
    model = StructuredModel(
        GoalResolution(
            intent=IntentResolution(type=TaskIntent.CODING),
            goal=GoalSpec(desc="fix memory leak"),
            plan=None,
        )
    )

    result = await resolve_goal_for_turn(
        model=model,
        user_text="继续",
        interaction_mode="auto",
        task_state=task_state,
    )

    assert result.goal is not None
    assert result.goal.desc == "fix memory leak"
