"""Regression tests for core graph behavior."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

import voidx.memory.store as store

from voidx.agent.agents import (
    AgentDef,
    child_agent_descriptions_for_llm,
    get_agent,
    get_visible_agents,
)
from voidx.agent.prompts import BASE_SYSTEM, PERSONA_MODEL, persona_prompt
from voidx.agent.graph.convergence import is_step_hint_message
from voidx.agent.graph.runtime import current_parent_tool_call_id
from voidx.agent.graph.runtime_guards import RuntimeGuardState, WallClockGuardState
from voidx.agent.graph import VoidXGraph
from voidx.agent.graph.tool_execution import AGENT_RESULT_PREVIEW_CHARS, _agent_result_preview
from voidx.agent.message_rows import RowMessageCacheEntry
from voidx.agent.runtime_context import InteractionMode, RuntimeContextBuilder
from voidx.config import Config, ParallelSubagentsConfig, Settings, UserProfile
from voidx.llm.compaction import CompactionSelection
from voidx.llm.instruction import InstructionService, WorkflowRuntimeContext
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
from voidx.agent.task_state import TaskState, ToolStatePatch, WorkflowRoute
from voidx.tools.base import ToolContext, ToolResult
from voidx.tools.agent import AgentResultContract, AgentTool
from voidx.tools.registry import ToolRegistry
from voidx.ui.output.dock import BottomInputDock, set_dock
from voidx.ui.output.events import DockEventConsumer, TurnStarted, ui_events


def _graph(tmp_path):
    cfg = Config(workspace=str(tmp_path))
    return VoidXGraph(cfg, api_key=None)


def _task_state_json(**kwargs):
    return TaskState(**kwargs).model_dump(mode="json")


def _edit_args(file_path: str) -> dict:
    return {
        "file_path": file_path,
        "edits": [{"operation": "replace", "lineno": 1, "prefix": "old", "suffix": "old", "new_string": "new"}],
    }


def _result_task_state(result: dict) -> TaskState:
    return TaskState.model_validate(result["task_state"])


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


@pytest.mark.asyncio
async def test_workflow_done_stops_before_followup_llm_when_workflow_complete(tmp_path):
    graph = _graph(tmp_path)
    parent = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "workflow",
                "args": {
                    "action": "done",
                    "workflow": "verify",
                    "evidence": "focused verification passed",
                    "summary": "verification complete",
                },
                "id": "call_adv",
                "type": "tool_call",
            },
        ],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
        "interaction_mode": "auto",
        "task_state": _task_state_json(
            current_intent=TaskIntent.CODING,
            workflow_runs={
                "verify": WorkflowRunState(name="verify", status=WorkflowRunStatus.ACTIVE),
            },
        ),
    })

    assert result["should_continue"] is False
    by_name = {run.name: run for run in _result_task_state(result).workflow_runs.values()}
    assert by_name["verify"].status == WorkflowRunStatus.SATISFIED


@pytest.mark.asyncio
async def test_workflow_route_end_satisfies_without_successor(tmp_path):
    graph = _graph(tmp_path)
    parent = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "workflow",
                "args": {
                    "action": "advance",
                    "workflow": "review",
                    "condition": "review_has_issues",
                    "evidence": "review verdict failed with actionable issues",
                    "summary": "review completed",
                },
                "id": "call_adv",
                "type": "tool_call",
            },
        ],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
        "interaction_mode": "auto",
        "task_state": _task_state_json(
            current_intent=TaskIntent.CODING,
            workflow_route={"join": "review", "leave": "review"},
            workflow_runs={
                "review": WorkflowRunState(name="review", status=WorkflowRunStatus.ACTIVE),
            },
        ),
    })

    assert "review_has_issues" in result["messages"][0].content
    assert result["should_continue"] is False
    by_name = {run.name: run for run in _result_task_state(result).workflow_runs.values()}
    assert by_name["review"].status == WorkflowRunStatus.SATISFIED
    assert "feedback" not in by_name


@pytest.mark.asyncio
async def test_workflow_route_end_satisfies_non_review_without_successor(tmp_path):
    graph = _graph(tmp_path)
    parent = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "workflow",
                "args": {
                    "action": "advance",
                    "workflow": "tdd",
                    "condition": "implemented",
                    "evidence": "implementation complete with focused test coverage",
                    "summary": "implementation complete",
                },
                "id": "call_adv",
                "type": "tool_call",
            },
        ],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
        "interaction_mode": "auto",
        "task_state": _task_state_json(
            current_intent=TaskIntent.CODING,
            workflow_route={"join": "tdd", "leave": "tdd"},
            workflow_runs={
                "tdd": WorkflowRunState(name="tdd", status=WorkflowRunStatus.ACTIVE),
            },
        ),
    })

    assert "implemented" in result["messages"][0].content
    assert result["should_continue"] is False
    by_name = {run.name: run for run in _result_task_state(result).workflow_runs.values()}
    assert by_name["tdd"].status == WorkflowRunStatus.SATISFIED
    assert "verify" not in by_name


@pytest.mark.asyncio
async def test_multiple_workflow_done_calls_finish_batch_before_stopping(tmp_path):
    graph = _graph(tmp_path)
    parent = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "workflow",
                "args": {
                    "action": "done",
                    "workflow": "design",
                    "evidence": "design doc archived",
                    "summary": "design doc complete",
                },
                "id": "call_design_done",
                "type": "tool_call",
            },
            {
                "name": "workflow",
                "args": {
                    "action": "done",
                    "workflow": "verify",
                    "evidence": "archive file exists and source file is removed",
                    "summary": "archive verification complete",
                },
                "id": "call_verify_done",
                "type": "tool_call",
            },
        ],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
        "interaction_mode": "auto",
        "task_state": _task_state_json(
            current_intent=TaskIntent.CODING,
            workflow_runs={
                "design": WorkflowRunState(name="design", status=WorkflowRunStatus.ACTIVE),
                "verify": WorkflowRunState(name="verify", status=WorkflowRunStatus.ACTIVE),
            },
        ),
    })

    assert [message.tool_call_id for message in result["messages"]] == [
        "call_design_done",
        "call_verify_done",
    ]
    assert result["should_continue"] is False
    by_name = {run.name: run for run in _result_task_state(result).workflow_runs.values()}
    assert by_name["design"].status == WorkflowRunStatus.SATISFIED
    assert by_name["verify"].status == WorkflowRunStatus.SATISFIED


