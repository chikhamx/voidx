"""Tests for run loop title generation, LSP, compaction, and misc."""

from voidx.agent.domain.turn_context import TurnExecutionContext
import asyncio
import contextlib
import sys
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
import voidx.persistence.sqlite as store


from voidx.agent.slash import SlashHandler
from voidx.agent.adapters.langgraph.execution import LangGraphExecution
from tests.langgraph_execution import make_langgraph_execution
from voidx.agent.application.agent_service import AgentService
from voidx.agent.adapters.langgraph.execution import _sanitize_generated_title
from voidx.agent.application.runtime_context import InteractionMode, TaskIntent
from voidx.agent.application.automation.goal.goal_resolver import ResolverGoal
from voidx.agent.domain.task.state import (
    GoalResolution,
    GoalSpec,
    IntentResolution,
    PlanResolution,
    TaskState,
)
from voidx.config import Config
from voidx.llm.usage import UsageStats
from voidx.agent.adapters.persistence.runtime_state_repository import RuntimeStateSnapshot, save_runtime_state
from voidx.agent.adapters.persistence.session_repository import MessageRow, create_session, get_session, load_messages, save_message, update_title
from voidx.update.service import UpdateCheckResult
from voidx.agent.application.automation.workflow.runtime import WorkflowActivationSource, WorkflowRunState, WorkflowRunStatus
from voidx.agent.application.runtime.task_tracker import TaskTracker
from voidx.presentation.output.dock import BottomInputDock, set_dock
from voidx.presentation.output.events import DockEventConsumer, ui_events
from voidx.presentation.protocol import UiSubmitCommand
from tests.presentation_ui import make_presentation_ui

runtime_ui_port = make_presentation_ui()
from tests.test_agent.adapters.langgraph.runtime.run_loop_helpers import (
    FakeTui,
    ExitTui,
    NoopMcpManager,
    NoopLspManager,
    _graph,
    _disable_external_managers,
)

@pytest.mark.asyncio
async def test_run_turn_keeps_default_title_when_resolver_falls_back_without_goal(tmp_path):
    graph = make_langgraph_execution(Config(workspace=str(tmp_path)), api_key=None)

    class FailingResolverModel:
        async def ainvoke(self, _messages):
            raise RuntimeError("resolver failed")

    class FakeGraph:
        async def astream(self, initial, _config, *, stream_mode="values"):
            yield {"messages": list(initial["messages"]) + [AIMessage(content="ok")], "task_state": initial["task_state"]}

    graph.model = FailingResolverModel()
    graph.graph = FakeGraph()
    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    try:
        await graph.run_turn("review runtime", context=TurnExecutionContext(thread_id=getattr(graph, "session_id", "") or "coding", session_id=getattr(graph, "session_id", "") or ""))
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)

    assert graph._session is not None
    assert graph._session.title == "New session"


@pytest.mark.asyncio
async def test_smart_title_generation_failure_keeps_temporary_title(tmp_path, monkeypatch):
    graph = make_langgraph_execution(Config(workspace=str(tmp_path)), api_key=None)

    def _fake_build_goal_resolution(user_text, task_state):
        return GoalResolution(
            intent=IntentResolution(type=TaskIntent.CODING),
            goal=GoalSpec(desc="分析启动流程"),
            plan=None,
        )

    class FakeGraph:
        async def astream(self, initial, _config, *, stream_mode="values"):
            yield {"messages": list(initial["messages"]) + [AIMessage(content="ok")]}

    graph.graph = FakeGraph()
    graph._interaction_mode = InteractionMode.GOAL
    import voidx.agent.adapters.langgraph.runtime.turn_runner as turn_runner_mod
    monkeypatch.setattr(turn_runner_mod, "build_goal_resolution", _fake_build_goal_resolution)
    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    set_dock(test_dock)
    graph.graph = FakeGraph()
    graph._interaction_mode = InteractionMode.GOAL
    test_dock = BottomInputDock()
    graph.graph = FakeGraph()
    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    try:
        await graph.run_turn("分析一下启动流程", context=TurnExecutionContext(thread_id=getattr(graph, "session_id", "") or "coding", session_id=getattr(graph, "session_id", "") or ""))
        task = graph._title_task
        if task is not None:
            await task

        assert graph._session is not None
        loaded = await get_session(graph._session.id)
        assert loaded is not None
        assert loaded.title == "分析启动流程"
        assert graph._session.title == "分析启动流程"
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)


@pytest.mark.asyncio
async def test_smart_title_does_not_override_manual_title(tmp_path):
    session = await create_session(workspace=str(tmp_path), provider="mimo", model="mimo-v2.5")
    await update_title(session.id, "temporary")
    session = session.model_copy(update={"title": "temporary"})
    graph = make_langgraph_execution(Config(workspace=str(tmp_path)), api_key=None, session=session)
    graph._schedule_session_title_generation(session.id, "first request", "temporary")
    assert graph._title_task is None

    await graph.set_session_title("Manual title")

    loaded = await get_session(session.id)
    assert loaded is not None
    assert loaded.title == "Manual title"


@pytest.mark.asyncio
async def test_smart_title_does_not_update_after_clear(tmp_path):
    session = await create_session(workspace=str(tmp_path), provider="mimo", model="mimo-v2.5")
    await update_title(session.id, "temporary")
    session = session.model_copy(update={"title": "temporary"})
    graph = make_langgraph_execution(Config(workspace=str(tmp_path)), api_key=None, session=session)
    graph._schedule_session_title_generation(session.id, "first request", "temporary")
    assert graph._title_task is None

    await graph.clear_current_session()
    if graph._clear_session_tasks:
        await asyncio.gather(*graph._clear_session_tasks)

    loaded = await get_session(session.id)
    assert graph._session is None
    assert loaded is not None
    assert loaded.title == "New session"


@pytest.mark.asyncio
async def test_smart_title_does_not_update_resumed_session(tmp_path):
    session = await create_session(workspace=str(tmp_path), provider="mimo", model="mimo-v2.5")
    await update_title(session.id, "temporary")
    session = session.model_copy(update={"title": "temporary"})
    resumed = await create_session(workspace=str(tmp_path), provider="mimo", model="mimo-v2.5")
    await update_title(resumed.id, "Resumed title")
    resumed = resumed.model_copy(update={"title": "Resumed title"})
    graph = make_langgraph_execution(Config(workspace=str(tmp_path)), api_key=None, session=session)
    graph._schedule_session_title_generation(session.id, "first request", "temporary")
    assert graph._title_task is None

    await graph.resume_session(resumed)

    loaded_old = await get_session(session.id)
    loaded_resumed = await get_session(resumed.id)
    assert graph._session is not None
    assert graph._session.id == resumed.id
    assert loaded_old is not None
    assert loaded_old.title == "temporary"
    assert loaded_resumed is not None
    assert loaded_resumed.title == "Resumed title"
