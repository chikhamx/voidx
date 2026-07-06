"""Tests for run loop title generation, LSP, compaction, and misc."""

import asyncio
import contextlib
import sys
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
import voidx.memory.store as store


from voidx.agent.slash import SlashHandler
from voidx.agent.graph import VoidXGraph
from voidx.agent.graph.run_loop import GraphRunLoopMixin
from voidx.agent.graph.title_mixin import _sanitize_generated_title
from voidx.agent.runtime_context import InteractionMode, TaskIntent
from voidx.agent.task_state import (
    GoalResolution,
    GoalSpec,
    IntentResolution,
    PlanResolution,
    TaskState,
)
from voidx.config import Config
from voidx.llm.usage import UsageStats
from voidx.memory.runtime_state import RuntimeStateSnapshot, save_runtime_state
from voidx.memory.session import MessageRow, create_session, get_session, load_messages, save_message, update_title
from voidx.selfupdate import UpdateCheckResult
from voidx.workflow.runtime import WorkflowActivationSource, WorkflowRunState, WorkflowRunStatus
from voidx.tools.task_tracker import TaskTracker
from voidx.ui.output.dock import BottomInputDock, set_dock
from voidx.ui.output.events import DockEventConsumer, ui_events
from voidx.ui.protocol import UiSubmitCommand
from voidx.runtime.ui_port import runtime_ui_port
from tests.test_agent.graph.run_loop_helpers import (
    FakeTui,
    ExitTui,
    NoopMcpManager,
    NoopLspManager,
    _graph,
    _disable_external_managers,
)

@pytest.mark.asyncio
async def test_run_once_uses_user_text_for_first_session_title_without_resolver_goal(tmp_path):
    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None)

    class StructuredGoalModel:
        def with_structured_output(self, schema):
            assert schema is GoalResolution
            return self

        async def ainvoke(self, _messages):
            return GoalResolution(
                intent=IntentResolution(type=TaskIntent.CODING),
                goal=None,
                plan=None,
            )

    class FakeGraph:
        async def ainvoke(self, initial, _config):
            return {"messages": list(initial["messages"]) + [AIMessage(content="ok")], "task_state": initial["task_state"]}

    graph.model = StructuredGoalModel()
    graph.graph = FakeGraph()
    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    try:
        await graph._run_once("review runtime")
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)

    assert graph._session is not None
    assert graph._session.title == "review runtime"


@pytest.mark.asyncio
async def test_smart_title_generation_failure_keeps_temporary_title(tmp_path):
    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None)

    class FailingTitleModel:
        async def ainvoke(self, _messages):
            raise RuntimeError("title failed")

    class FakeGraph:
        async def ainvoke(self, initial, _config):
            return {"messages": list(initial["messages"]) + [AIMessage(content="ok")]}

    graph.model = FailingTitleModel()
    graph.graph = FakeGraph()
    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    try:
        await graph._run_once("分析一下启动流程")
        task = graph._title_task
        if task is not None:
            await task

        assert graph._session is not None
        loaded = await get_session(graph._session.id)
        assert loaded is not None
        assert loaded.title == "分析一下启动流程"
        assert graph._session.title == "分析一下启动流程"
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)


@pytest.mark.asyncio
async def test_smart_title_does_not_override_manual_title(tmp_path):
    session = await create_session(workspace=str(tmp_path), provider="mimo", model="mimo-v2.5")
    await update_title(session.id, "temporary")
    session = session.model_copy(update={"title": "temporary"})
    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None, session=session)
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
    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None, session=session)
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
    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None, session=session)
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


