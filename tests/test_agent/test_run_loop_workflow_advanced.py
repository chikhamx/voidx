"""Tests for run loop run_once workflow resolution."""

import asyncio
import contextlib
import sys
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
import voidx.memory.store as store

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from voidx.agent.slash import SlashHandler
from voidx.agent.graph import VoidXGraph
from voidx.agent.graph.run_loop import GraphRunLoopMixin
from voidx.agent.graph.title_mixin import _sanitize_generated_title
from voidx.agent.runtime_context import InteractionMode, TaskIntent
from voidx.agent.task_state import (
    GoalResolution,
    GoalSpec,
    GoalType,
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
from tests.test_agent._run_loop_helpers import (
    FakeTui,
    ExitTui,
    NoopMcpManager,
    NoopLspManager,
    _graph,
    _disable_external_managers,
)


@pytest.mark.asyncio
async def test_run_once_clears_stale_completed_workflow_when_resolver_has_no_join(tmp_path):
    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None)
    graph._task_state = TaskState(
        current_goal=GoalSpec(type=GoalType.CHORE, desc="检查检查，准备push吧"),
        workflow_runs={
            "verify": WorkflowRunState(
                name="verify",
                status=WorkflowRunStatus.SATISFIED,
                reason="transition from tdd via implemented",
            )
        },
    )
    captured: dict[str, object] = {}

    class StructuredGoalModel:
        def with_structured_output(self, schema):
            assert schema is GoalResolution
            return self

        async def ainvoke(self, messages):
            assert "GoalResolution JSON schema" not in messages[0].content
            assert messages[-1].content == "检查检查，准备push吧"
            return {
                "intent": {"type": "coding", "desc": "plain follow-up request"},
                "goal": None,
                "plan": None,
            }

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
        await graph._run_once("检查检查，准备push吧")
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)

    initial = captured["initial"]
    state = TaskState.model_validate(initial["task_state"])
    assert state.workflow_runs == {}
    assert initial["persona"] == "implement"


@pytest.mark.asyncio
async def test_run_once_preadvances_workflow_from_resolver_workflow_start(tmp_path):
    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None)
    graph._task_state = TaskState(
        current_goal=GoalSpec(type=GoalType.DESIGN, desc="agent_name 语义清理"),
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
        def with_structured_output(self, schema):
            assert schema is GoalResolution
            return self

        async def ainvoke(self, messages):
            assert "GoalResolution JSON schema" not in messages[0].content
            assert messages[-1].content == "可以，先写一个 spec"
            return {
                "intent": {"type": "coding", "desc": "user requested spec"},
                "goal": {"type": "doc", "desc": "agent_name 语义清理"},
                "plan": {"join": "design", "leave": "design"},
            }

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
        await graph._run_once("可以，先写一个 spec")
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)

    initial = captured["initial"]
    state = TaskState.model_validate(initial["task_state"])
    assert "brainstorm" not in state.workflow_runs
    assert state.workflow_runs["design"].status == WorkflowRunStatus.ACTIVE
    assert state.workflow_runs["design"].reason == "resolver plan.join"
    assert initial["persona"] == "plan"


