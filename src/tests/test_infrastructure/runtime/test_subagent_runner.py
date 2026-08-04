"""Regression tests for core graph behavior."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace


import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

import voidx.memory.store as store

from voidx.agent.application.agents import (
    AgentDef,
    child_agent_descriptions_for_llm,
    get_agent,
    get_visible_agents,
)
from voidx.agent.application.prompts import BASE_SYSTEM, PERSONA_MODEL, persona_prompt
from voidx.agent.infrastructure.langgraph.runtime.convergence import is_step_hint_message
from voidx.agent.infrastructure.langgraph.runtime.runtime import current_parent_tool_call_id
from voidx.agent.infrastructure.langgraph.runtime.runtime_guards import RuntimeGuardState, WallClockGuardState
from voidx.agent.infrastructure.langgraph.execution import LangGraphExecution
from voidx.agent.infrastructure.langgraph.execution import AGENT_RESULT_PREVIEW_CHARS, _agent_result_preview
from voidx.agent.infrastructure.message_rows import RowMessageCacheEntry
from voidx.agent.application.runtime_context import InteractionMode, RuntimeContextBuilder
from voidx.config import Config, Settings, UserProfile
from voidx.llm.compaction import CompactionSelection
from voidx.agent.application.instruction import InstructionService, WorkflowRuntimeContext
from voidx.memory.session import (
    MessageRow,
    SessionInfo,
    create_session,
    delete_session,
    load_messages,
    save_message,
)
from voidx.memory.transcript import load_transcript
from voidx.permission.service import PermissionService
from voidx.runtime import GoalResolution, GoalSpec, IntentResolution, PlanResolution, TaskIntent
from voidx.skills.context import SKILL_TOOL_CONTEXT_MARKER
from voidx.workflow.context import WORKFLOW_CONTEXT_MARKER
from voidx.workflow.runtime import WorkflowRunState, WorkflowRunStatus
from voidx.runtime.task_state import TaskState, ToolStatePatch, WorkflowRoute
from voidx.tools.base import ToolContext, ToolResult
from voidx.tools.agent import AgentResultContract, AgentTool
from voidx.tools.registry import ToolRegistry
from voidx.ui.output.dock import BottomInputDock, set_dock
from voidx.ui.output.events import DockEventConsumer, TurnStarted, ui_events


def _graph(tmp_path):
    cfg = Config(workspace=str(tmp_path))
    return LangGraphExecution(cfg, api_key=None)


def _task_state_json(**kwargs):
    return TaskState(**kwargs).model_dump(mode="json")


def _edit_args(file_path: str) -> dict:
    return {
        "file_path": file_path,
        "edits": [{"operation": "replace", "lineno": 1, "prefix": "old", "suffix": "old", "new_string": "new"}],
    }


def _result_task_state(result: dict) -> TaskState:
    return TaskState.model_validate(result["task_state"])


def _asked_tool_calls(batch):
    return [getattr(item, "tool_call", item) for item in batch]


def _child_goal_resolution(
    goal_type: str = "feature",
    *,
    desc: str = "Implement the feature",
    join: str = "tdd",
    leave: str = "verify",
) -> GoalResolution:
    return GoalResolution(
        intent=IntentResolution(type=TaskIntent.CODING),
        goal=GoalSpec(desc=desc),
        plan=PlanResolution(join=join, leave=leave),
    )


def _child_result_contract(schema_name: str = "implementation_result") -> AgentResultContract:
    result_format = (
        "verdict=PASS|FAIL|NEEDS_CHANGE, findings, risks, verification_notes, next_actions"
        if schema_name == "review_result"
        else "status, files_changed, tests_run, risks, followups"
    )
    return AgentResultContract(
        schema_name=schema_name,
        format=result_format,
    )


def _subagent_contract_kwargs(
    *,
    goal_type: str = "inspect",
    desc: str = "Inspect the workspace",
    join: str = "review",
    leave: str = "review",
    schema_name: str = "inspection_result",
) -> dict:
    return {
        "goal_resolution": _child_goal_resolution(goal_type, desc=desc, join=join, leave=leave),
        "result_contract": _child_result_contract(schema_name),
    }


@pytest.fixture(autouse=True)
def isolated_memory_store(tmp_path):
    if store._conn is not None:
        store._conn.close()
    store._conn = None
    store.DATA_DIR = tmp_path / ".voidx"
    yield
    if store._conn is not None:
        store._conn.close()
    store._conn = None


def _tree_nodes(root):
    nodes = [root]
    for child in root.children:
        nodes.extend(_tree_nodes(child))
    return nodes


def test_run_subagent_uses_workflow_contract_instead_of_model_budget():
    import inspect

    from voidx.agent.infrastructure.langgraph.runtime.subagent import run_subagent

    parameters = inspect.signature(run_subagent).parameters
    assert "max_steps" not in parameters
    assert parameters["goal_resolution"].default is inspect.Parameter.empty
    assert parameters["result_contract"].default is inspect.Parameter.empty


@pytest.mark.asyncio
async def test_subagent_runner_passes_main_workflow_runtime_context(tmp_path, monkeypatch):
    import voidx.agent.infrastructure.langgraph.execution as core_module
    from voidx.runtime.ui_port import RuntimeUiPort

    graph = _graph(tmp_path)
    goal_resolution = _child_goal_resolution()
    result_contract = _child_result_contract()
    expected_context = WorkflowRuntimeContext(
        instructions=["instruction"],
        active=["tdd (implement persona)"],
        content="skill context",
        runs=[
            WorkflowRunState(
                name="tdd",
                status=WorkflowRunStatus.ACTIVE,
                goal_type="feature",
                scope="Implement the feature",
                personas=["implement"],
            )
        ],
    )
    calls: list[dict] = []
    captured: dict[str, object] = {}
    emitted: list[object] = []

    class RecordingEvents:
        async def emit(self, event):
            emitted.append(event)

    async def fake_workflow_context_for(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return expected_context

    async def fake_run_subagent(*_args, **kwargs):
        captured.update(kwargs)
        return "child result"

    graph._instruction.workflow_context_for = fake_workflow_context_for
    graph._ui.__dict__.pop("via_events", None)
    monkeypatch.setattr(RuntimeUiPort, "events", property(lambda _self: RecordingEvents()))
    monkeypatch.setattr(RuntimeUiPort, "via_events", lambda _self: True)
    monkeypatch.setattr(core_module, "_run_subagent", fake_run_subagent)

    result = await graph._subagent_runner(
        get_agent("voidx"),
        "Implement the feature",
        goal_resolution,
        result_contract,
    )

    assert result == "child result"
    assert captured["goal_resolution"] == goal_resolution
    assert captured["result_contract"] == result_contract
    assert captured["runtime_persona"] == "implement"
    assert captured["workflow_runtime_context"] is expected_context
    assert "skill_selection" not in captured
    assert ("parent" + "_messages") not in captured
    assert emitted[-1].kind == "subagent.finished"
    assert emitted[-1].summary == "child result"
    assert "agent" not in calls[0]["kwargs"]
    assert "task_intent" not in calls[0]["kwargs"]
    assert calls[0]["kwargs"]["goal_type"] == "feature"
    assert calls[0]["kwargs"]["scope"] == "Implement the feature"
    assert calls[0]["kwargs"]["workflow_start"] == "tdd"




@pytest.mark.asyncio
async def test_subagent_runner_reports_initialization_error(tmp_path, monkeypatch):
    import voidx.agent.infrastructure.langgraph.execution as core_module
    from voidx.runtime.ui_port import RuntimeUiPort

    graph = _graph(tmp_path)
    emitted: list[object] = []

    class RecordingEvents:
        async def emit(self, event):
            emitted.append(event)

    async def fail_workflow_context_for(*_args, **_kwargs):
        raise RuntimeError("provider schema rejected tool definitions")

    graph._instruction.workflow_context_for = fail_workflow_context_for
    graph._ui.__dict__.pop("via_events", None)
    monkeypatch.setattr(RuntimeUiPort, "events", property(lambda _self: RecordingEvents()))
    monkeypatch.setattr(RuntimeUiPort, "via_events", lambda _self: True)

    with pytest.raises(RuntimeError, match="provider schema rejected tool definitions"):
        await graph._subagent_runner(
            get_agent("voidx"),
            "Inspect the backend",
            _child_goal_resolution("inspect"),
            _child_result_contract("inspection_result"),
        )

    assert emitted[-1].kind == "subagent.finished"
    assert emitted[-1].ok is False
    assert emitted[-1].error == "provider schema rejected tool definitions"


@pytest.mark.asyncio
async def test_subagent_runner_persists_lifecycle_jsonl(tmp_path, monkeypatch):
    import voidx.agent.infrastructure.langgraph.execution as core_module

    graph = _graph(tmp_path)
    graph._session = await create_session(workspace=str(tmp_path))
    goal_resolution = _child_goal_resolution("inspect", desc="Inspect storage design", join="review", leave="review")
    result_contract = _child_result_contract("inspection_result")

    async def fake_workflow_context_for(*_args, **_kwargs):
        return WorkflowRuntimeContext(
            instructions=[],
            active=[],
            content="",
            runs=[
                WorkflowRunState(
                    name="review",
                    status=WorkflowRunStatus.ACTIVE,
                    goal_type="inspect",
                    scope="Inspect storage design",
                    personas=["review"],
                )
            ],
        )

    async def fake_run_subagent(*_args, **kwargs):
        kwargs["run_metadata"].update({
            "finish_reason": "final_answer",
        })
        return "child result"

    graph._instruction.workflow_context_for = fake_workflow_context_for
    monkeypatch.setattr(core_module, "_run_subagent", fake_run_subagent)

    try:
        result = await graph._subagent_runner(
            get_agent("voidx"),
            "Inspect storage design",
            goal_resolution,
            result_contract,
        )

        path = store.DATA_DIR / "sessions" / graph._session.id / "subagents" / "agent_0.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        assert result == "child result"
        assert [row["type"] for row in rows] == ["subagent_start", "subagent_finish"]
        assert rows[0]["agent_run_id"] == "agent_0"
        assert rows[0]["persona"] == "review"
        assert rows[0]["description"] == "Inspect storage design"
        assert rows[0]["goal_resolution"]["goal"]["desc"] == "Inspect storage design"
        assert rows[0]["result_schema"] == "inspection_result"
        assert rows[1]["ok"] is True
        assert rows[1]["finish_reason"] == "final_answer"
    finally:
        await delete_session(graph._session.id)


@pytest.mark.asyncio
async def test_subagent_runner_authorizes_with_child_interaction_mode(tmp_path, monkeypatch):
    import voidx.agent.infrastructure.langgraph.execution as core_module

    graph = _graph(tmp_path)
    authorize_calls: list[dict] = []
    goal_resolution = _child_goal_resolution("design", desc="Plan the feature", join="plan", leave="plan")
    result_contract = _child_result_contract("design_result")

    async def fake_workflow_context_for(*_args, **_kwargs):
        return WorkflowRuntimeContext(
            instructions=[],
            active=[],
            content="",
            runs=[
                WorkflowRunState(
                    name="plan",
                    status=WorkflowRunStatus.ACTIVE,
                    goal_type="design",
                    scope="Plan the feature",
                    personas=["plan"],
                )
            ],
        )

    async def fake_authorize(tool_calls, **kwargs):
        authorize_calls.append(kwargs)
        return tool_calls, []

    async def fake_run_subagent(*_args, **kwargs):
        await kwargs["authorize_tools"]([])
        return "child result"

    graph._instruction.workflow_context_for = fake_workflow_context_for
    graph._authorize_tool_calls = fake_authorize
    monkeypatch.setattr(core_module, "_run_subagent", fake_run_subagent)

    result = await graph._subagent_runner(
        get_agent("voidx"),
        "Plan the feature",
        goal_resolution,
        result_contract,
    )

    assert result == "child result"
    assert authorize_calls[0]["plan_mode"] is True
    assert authorize_calls[0]["interaction_mode"] == "plan"


@pytest.mark.asyncio
async def test_graph_authorization_does_not_treat_goal_as_read_only_mode(tmp_path):
    graph = _graph(tmp_path)
    asked: list[list[dict]] = []

    async def approve(tool_calls):
        asked.append(tool_calls)
        return "y"

    graph._ask_tool_permission = approve

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "edit", "args": {"file_path": "src/app.py"}, "id": "call_1"}],
        runtime_persona="implement",
        plan_mode=False,
        session_id="test",
        interaction_mode="goal",
    )

    assert [tc["name"] for tc in approved] == ["replace"]
    assert denied == []
    assert [[tc["name"] for tc in _asked_tool_calls(batch)] for batch in asked] == [["replace"]]


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_graph_authorization_allows_read_only_bash(tmp_path):
    graph = _graph(tmp_path)

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "bash", "args": {"command": "ls"}, "id": "call_1"}],
        plan_mode=False,
        session_id="test",
        interaction_mode="auto",
    )

    assert [tc["name"] for tc in approved] == ["bash"]
    assert denied == []


@pytest.mark.asyncio
async def test_graph_authorization_prompts_for_edit(tmp_path):
    graph = _graph(tmp_path)
    asked: list[list[dict]] = []

    async def approve(tool_calls):
        asked.append(tool_calls)
        return "y"

    graph._ask_tool_permission = approve

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "edit", "args": {"file_path": "src/app.py"}, "id": "call_1"}],
        plan_mode=False,
        session_id="test",
        interaction_mode="auto",
    )

    assert [tc["name"] for tc in approved] == ["replace"]
    assert denied == []
    assert [[tc["name"] for tc in _asked_tool_calls(batch)] for batch in asked] == [["replace"]]


@pytest.mark.asyncio
async def test_graph_authorization_respects_session_allow_for_edit(tmp_path):
    graph = _graph(tmp_path)
    graph._permission.allow_silent("edit")

    async def fail_if_asked(_tool_calls):
        pytest.fail("session-allowed edit should not prompt")

    graph._ask_tool_permission = fail_if_asked

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "edit", "args": {"file_path": "src/app.py"}, "id": "call_1"}],
        plan_mode=False,
        session_id="test",
        interaction_mode="auto",
    )

    assert [tc["name"] for tc in approved] == ["replace"]
    assert denied == []


@pytest.mark.asyncio
async def test_graph_authorization_prompts_for_unsafe_bash(tmp_path):
    graph = _graph(tmp_path)
    asked: list[list[dict]] = []

    async def approve(tool_calls):
        asked.append(tool_calls)
        return "y"

    graph._ask_tool_permission = approve

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "bash", "args": {"command": "pip install requests"}, "id": "call_1"}],
        plan_mode=False,
        session_id="test",
        interaction_mode="auto",
    )

    assert [tc["name"] for tc in approved] == ["bash"]
    assert denied == []
    assert [[tc["name"] for tc in _asked_tool_calls(batch)] for batch in asked] == [["bash"]]
