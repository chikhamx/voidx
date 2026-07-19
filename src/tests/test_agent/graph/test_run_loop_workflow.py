"""Tests for run loop run_once workflow resolution."""

import asyncio
import contextlib
import sys
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
import voidx.memory.store as store


from voidx.agent.slash import SlashHandler
from voidx.agent.infrastructure.langgraph.execution import LangGraphExecution
from voidx.agent.application.agent_service import AgentService
from voidx.agent.infrastructure.langgraph.execution import _sanitize_generated_title
from voidx.agent.runtime_context import InteractionMode, TaskIntent
from voidx.runtime.task_state import (
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
async def test_first_turn_without_goal_uses_temporary_session_title(tmp_path):
    graph = LangGraphExecution(Config(workspace=str(tmp_path)), api_key=None)

    class StructuredGoalModel:
        def with_structured_output(self, schema):
            assert schema is GoalResolution
            return self

        async def ainvoke(self, messages):
            assert "看看这个项目" in messages[1].content
            return GoalResolution(
                intent=IntentResolution(type=TaskIntent.CODING),
                goal=None,
                plan=None,
            )

    class FakeGraph:
        async def ainvoke(self, initial, _config):
            return {"messages": list(initial["messages"]) + [AIMessage(content="ok")]}

    graph.model = StructuredGoalModel()
    graph.graph = FakeGraph()
    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    try:
        await graph.run_turn("看看这个项目")
        task = graph._title_task
        if task is not None:
            await task

        assert graph._session is not None
        loaded = await get_session(graph._session.id)
        assert loaded is not None
        assert loaded.title == "New session"
        assert graph._session.title == "New session"
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)


@pytest.mark.asyncio
async def test_run_turn_uses_general_fallback_when_structured_resolver_fails(tmp_path):
    graph = LangGraphExecution(Config(workspace=str(tmp_path)), api_key=None)
    captured: dict[str, object] = {}

    class StructuredGoalModel:
        def with_structured_output(self, _schema):
            raise RuntimeError("structured resolver unavailable")

        async def ainvoke(self, _messages):
            raise AssertionError("resolver should fail before invoking")

    class FakeGraph:
        async def ainvoke(self, initial, _config):
            captured["initial"] = initial
            return {"messages": list(initial["messages"]) + [AIMessage(content="ok")]}

    graph.model = StructuredGoalModel()
    graph.graph = FakeGraph()
    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    try:
        await graph.run_turn("review runtime context")
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)

    initial = captured["initial"]
    assert initial["task_state"]["current_intent"] == "coding"
    assert initial["task_state"]["current_goal"] is None
    assert initial["task_state"]["recent_exchanges"] == []
    rows = await load_messages(graph._session.id)
    assert [row.role for row in rows] == ["user", "assistant"]
    assert all("GoalResolution JSON schema" not in row.content for row in rows)


@pytest.mark.asyncio
async def test_run_turn_does_not_preadvance_workflow_without_resolver_join(tmp_path):
    graph = LangGraphExecution(Config(workspace=str(tmp_path)), api_key=None)
    graph._task_state = TaskState(
        current_goal=GoalSpec(desc="agent_name 语义清理"),
        workflow_runs={
            "brainstorm": WorkflowRunState(
                name="brainstorm",
                status=WorkflowRunStatus.ACTIVE,
                goal_type="design",
                scope="agent_name 语义清理",
            )
        },
    )
    captured: dict[str, object] = {}

    class StructuredGoalModel:
        def with_structured_output(self, _schema):
            raise RuntimeError("structured resolver unavailable")

        async def ainvoke(self, _messages):
            raise AssertionError("resolver should fail before invoking")

    class FakeGraph:
        async def ainvoke(self, initial, _config):
            captured["initial"] = initial
            return {"messages": list(initial["messages"]) + [AIMessage(content="ok")], "task_state": initial["task_state"]}

    graph.model = StructuredGoalModel()
    graph.graph = FakeGraph()
    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    try:
        await graph.run_turn("可以，先写一个 spec")
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)

    initial = captured["initial"]
    state = TaskState.model_validate(initial["task_state"])
    assert "brainstorm" in state.workflow_runs
    assert initial["persona"] == "coordinate"


