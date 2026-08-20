"""Tests for run loop run_once workflow resolution."""

from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.domain.agent_profile import WorkflowRuntimeContext as ProfileWorkflowRuntimeContext
from voidx.agent.domain.automation.workflow_dag import DEFAULT_WORKFLOW_DAG
import asyncio
import contextlib
import sys
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
import voidx.persistence.sqlite as store


from voidx.presentation.slash import SlashHandler
from voidx.agent.adapters.langgraph.execution import LangGraphExecution
from tests.langgraph_execution import make_langgraph_execution
from voidx.agent.application.agent_service import AgentService
from voidx.agent.adapters.langgraph.execution import _sanitize_generated_title
from voidx.agent.application.runtime_context import InteractionMode, TaskIntent
from voidx.agent.domain.task.state import GoalResolution, IntentResolution, PlanResolution, GoalSpec, TaskState
from voidx.agent.application.automation.goal.goal_resolver import ResolverGoal
from voidx.agent.domain.task.state import GoalSpec, TaskState
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


def _coding_turn_context(graph, tmp_path) -> TurnExecutionContext:
    identity = getattr(graph, "session_id", "") or "coding"
    return TurnExecutionContext(
        thread_id=identity,
        session_id=getattr(graph, "session_id", "") or "",
        workspace=str(tmp_path),
        workflow_context=ProfileWorkflowRuntimeContext(
            dag=DEFAULT_WORKFLOW_DAG,
            dag_revision=1,
            dag_hash="test-default-workflow",
            source="bundled",
        ),
    )
from tests.test_agent.adapters.langgraph.runtime.run_loop_helpers import (
    FakeTui,
    ExitTui,
    NoopMcpManager,
    NoopLspManager,
    _graph,
    _disable_external_managers,
)


@pytest.mark.asyncio
async def test_run_turn_clears_stale_completed_workflow_when_resolver_has_no_join(tmp_path):
    graph = make_langgraph_execution(Config(workspace=str(tmp_path)), api_key=None)
    graph._task_state = TaskState(
        current_goal=GoalSpec(desc="检查检查，准备push吧"),
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
            assert schema is ResolverGoal
            return self

        async def ainvoke(self, messages):
            assert "## ResolverGoal Schema" not in messages[-1].content
            assert "检查检查，准备push吧" in messages[-1].content
            return {
                "intent": "coding",
                "goal": None,
                "workflow": None,
                "kind_hint": "chore",
            }

    class FakeGraph:
        async def astream(self, initial, _config, *, stream_mode="values"):
            captured["initial"] = initial
            yield {"messages": list(initial["messages"]) + [AIMessage(content="ok")], "task_state": initial["task_state"]}

    graph.model = StructuredGoalModel()
    graph.graph = FakeGraph()
    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    try:
        await graph.run_turn("检查检查，准备push吧", context=_coding_turn_context(graph, tmp_path))
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)

    initial = captured["initial"]
    state = TaskState.model_validate(initial["task_state"])
    assert state.workflow_runs == {}
    assert initial["persona"] == "coordinate"


@pytest.mark.asyncio
async def test_run_turn_preadvances_workflow_from_resolver_workflow_start(tmp_path, monkeypatch):
    graph = make_langgraph_execution(Config(workspace=str(tmp_path)), api_key=None)
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

    def _fake_build_goal_resolution(user_text, task_state):
        return GoalResolution(
            intent=IntentResolution(type=TaskIntent.CODING),
            goal=GoalSpec(desc="agent_name 语义清理"),
            plan=PlanResolution(join="design", leave=None),
        )

    class FakeGraph:
        async def astream(self, initial, _config, *, stream_mode="values"):
            captured["initial"] = initial
            yield {"messages": list(initial["messages"]) + [AIMessage(content="ok")], "task_state": initial["task_state"]}

    graph.graph = FakeGraph()
    graph._interaction_mode = InteractionMode.GOAL
    import voidx.agent.adapters.langgraph.runtime.turn_runner as turn_runner_mod
    monkeypatch.setattr(turn_runner_mod, "build_goal_resolution", _fake_build_goal_resolution)

    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    try:
        await graph.run_turn("可以，先写一个 spec", context=_coding_turn_context(graph, tmp_path))
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)

    initial = captured["initial"]
    state = TaskState.model_validate(initial["task_state"])
    assert state.workflow_runs["brainstorm"].status == WorkflowRunStatus.SATISFIED
    assert state.workflow_runs["design"].status == WorkflowRunStatus.ACTIVE
    assert state.workflow_runs["design"].reason == "transition from brainstorm via approved"
    assert initial["persona"] == "plan"
