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
async def test_barrier_failure_blocks_following_tools(tmp_path):
    graph = _graph(tmp_path)
    executed: list[str] = []

    class FailingBarrierTool:
        id = "checkpoint"
        description = "fake failing barrier"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            executed.append("checkpoint")
            return ToolResult(output="barrier failed", metadata={"error": True})

    class ExplodingReadTool:
        id = "read"
        description = "fake read"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            pytest.fail("read should be blocked after failed barrier")

    graph.tools.register("checkpoint", FailingBarrierTool(), "fake failing barrier", {"type": "object", "properties": {}})
    graph.tools.register("read", ExplodingReadTool(), "fake read", {"type": "object", "properties": {}})

    async def allow_all(
        tool_calls,
        plan_mode: bool,
        session_id: str,
        interaction_mode=None,
    ):
        return tool_calls, []

    graph._authorize_tool_calls = allow_all
    parent = AIMessage(
        content="",
        tool_calls=[
            {"name": "checkpoint", "args": {}, "id": "call_plan", "type": "tool_call"},
            {"name": "read", "args": {"file_path": "src/app.py"}, "id": "call_read", "type": "tool_call"},
        ],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
    })

    assert executed == ["checkpoint"]
    assert [message.tool_call_id for message in result["messages"]] == ["call_plan", "call_read"]
    assert result["messages"][0].content == "barrier failed"
    assert result["messages"][1].content == "Blocked because a prior runtime barrier was failed."


@pytest.mark.asyncio
async def test_multiple_barriers_apply_patches_in_order(tmp_path):
    graph = _graph(tmp_path)
    observed: list[str] = []

    class FakeClarifyTool:
        id = "clarify"
        description = "fake clarify barrier"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            observed.append(f"clarify:{ctx.task_intent}:{ctx.goal_type}:{ctx.goal_target}")
            patch = ToolStatePatch(
                intent=IntentResolution(type=TaskIntent.CODING),
                goal=GoalSpec(desc="after intent"),
            )
            return ToolResult(
                output="clarify ok",
                metadata={"state_patch": patch.model_dump(mode="json", exclude_unset=True)},
            )

    class FakePlanTool:
        id = "checkpoint"
        description = "fake plan barrier"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            observed.append(f"checkpoint:{ctx.task_intent}:{ctx.goal_type}:{ctx.goal_target}")
            patch = ToolStatePatch(
                intent=IntentResolution(type=TaskIntent.CODING),
                goal=GoalSpec(desc="after plan"),
            )
            return ToolResult(
                output="plan ok",
                metadata={"state_patch": patch.model_dump(mode="json", exclude_unset=True)},
            )

    class RecordingReadTool:
        id = "read"
        description = "fake read"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            observed.append(f"read:{ctx.task_intent}:{ctx.goal_type}:{ctx.goal_target}")
            return ToolResult(output=f"read after barriers: {ctx.task_intent}:{ctx.goal_type}:{ctx.goal_target}")

    graph.tools.register("clarify", FakeClarifyTool(), "fake clarify", {"type": "object", "properties": {}})
    graph.tools.register("checkpoint", FakePlanTool(), "fake plan", {"type": "object", "properties": {}})
    graph.tools.register("read", RecordingReadTool(), "fake read", {"type": "object", "properties": {}})

    async def allow_all(
        tool_calls,
        plan_mode: bool,
        session_id: str,
        interaction_mode=None,
    ):
        return tool_calls, []

    graph._authorize_tool_calls = allow_all
    parent = AIMessage(
        content="",
        tool_calls=[
            {"name": "clarify", "args": {}, "id": "call_clarify", "type": "tool_call"},
            {"name": "checkpoint", "args": {}, "id": "call_plan", "type": "tool_call"},
            {"name": "read", "args": {"file_path": "src/app.py"}, "id": "call_read", "type": "tool_call"},
        ],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
        "interaction_mode": "auto",
        "task_state": _task_state_json(current_intent=TaskIntent.GENERAL),
    })

    assert [message.tool_call_id for message in result["messages"]] == [
        "call_clarify",
        "call_plan",
        "call_read",
    ]
    assert observed == [
        "clarify:general::",
        "checkpoint:coding::after intent",
        "read:coding::after plan",
    ]
    task_state = _result_task_state(result)
    assert task_state.current_intent == TaskIntent.CODING
    assert task_state.current_goal is not None
    assert task_state.current_goal.desc == "after plan"
    assert result["messages"][2].content == "read after barriers: coding::after plan"


@pytest.mark.asyncio
async def test_workflow_transaction_reauthorizes_following_write(tmp_path):
    graph = _graph(tmp_path)
    graph._permission.approval_policy = "on-request"
    invalidations = 0

    class FakeApp:
        def invalidate(self):
            nonlocal invalidations
            invalidations += 1

        async def ask_choice(self, _prompt, _choices, details=None):
            return "y"

    graph._app = FakeApp()
    parent = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "workflow",
                "args": {
                    "action": "advance",
                    "workflow": "brainstorm",
                    "condition": "small_change",
                    "goal": "执行小改动",
                    "summary": "design gate cleared",
                },
                "id": "call_adv",
                "type": "tool_call",
            },
            {
                "name": "manage",
                "args": {"op": "create", "paths": "tmp-repro.txt"},
                "id": "call_write",
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
                "brainstorm": WorkflowRunState(name="brainstorm", status=WorkflowRunStatus.ACTIVE),
            },
        ),
    })

    assert [message.tool_call_id for message in result["messages"]] == ["call_adv", "call_write"]
    assert "Blocked by workflow gate" not in result["messages"][1].content
    assert (tmp_path / "tmp-repro.txt").exists()
    by_name = {run.name: run for run in _result_task_state(result).workflow_runs.values()}
    assert by_name["brainstorm"].status == WorkflowRunStatus.SATISFIED
    assert graph._task_state.workflow_runs["brainstorm"].status == WorkflowRunStatus.SATISFIED
    assert invalidations > 0

