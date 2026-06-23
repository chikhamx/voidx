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
async def test_parallel_subagents_disabled_serializes_agent_calls(tmp_path):
    graph = _graph(tmp_path)
    active = 0
    max_active = 0

    class FakeAgentTool:
        id = "agent"
        description = "fake agent"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            nonlocal active, max_active
            call_id = current_parent_tool_call_id.get()
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return ToolResult(output=f"done {call_id}")

    graph.tools.register("agent", FakeAgentTool(), "fake agent", {"type": "object", "properties": {}})

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
            {"name": "agent", "args": {"description": "a"}, "id": "call_a", "type": "tool_call"},
            {"name": "agent", "args": {"description": "b"}, "id": "call_b", "type": "tool_call"},
        ],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
    })

    messages = result["messages"]
    assert max_active == 1
    assert [msg.tool_call_id for msg in messages if isinstance(msg, ToolMessage)] == ["call_a", "call_b"]


@pytest.mark.asyncio
async def test_parallel_subagents_enabled_runs_agent_calls_concurrently(tmp_path):
    graph = VoidXGraph(
        Config(
            workspace=str(tmp_path),
            parallel_subagents=ParallelSubagentsConfig(enabled=True, max_concurrent=4),
        ),
        api_key=None,
    )
    active = 0
    max_active = 0

    class FakeAgentTool:
        id = "agent"
        description = "fake agent"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            nonlocal active, max_active
            call_id = current_parent_tool_call_id.get()
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return ToolResult(output=f"done {call_id}")

    graph.tools.register("agent", FakeAgentTool(), "fake agent", {"type": "object", "properties": {}})

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
            {"name": "agent", "args": {"description": "a"}, "id": "call_a", "type": "tool_call"},
            {"name": "agent", "args": {"description": "b"}, "id": "call_b", "type": "tool_call"},
        ],
    )

    await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
    })

    assert max_active == 2


@pytest.mark.asyncio
async def test_parallel_subagents_preserves_tool_message_order(tmp_path):
    graph = VoidXGraph(
        Config(
            workspace=str(tmp_path),
            parallel_subagents=ParallelSubagentsConfig(enabled=True, max_concurrent=2),
        ),
        api_key=None,
    )

    class FakeAgentTool:
        id = "agent"
        description = "fake agent"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            call_id = current_parent_tool_call_id.get()
            if call_id == "call_a":
                await asyncio.sleep(0.02)
            return ToolResult(output=f"done {call_id}")

    graph.tools.register("agent", FakeAgentTool(), "fake agent", {"type": "object", "properties": {}})

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
            {"name": "agent", "args": {"description": "a"}, "id": "call_a", "type": "tool_call"},
            {"name": "agent", "args": {"description": "b"}, "id": "call_b", "type": "tool_call"},
        ],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
    })

    tool_messages = [msg for msg in result["messages"] if isinstance(msg, ToolMessage)]
    assert [msg.tool_call_id for msg in tool_messages] == ["call_a", "call_b"]
    assert [msg.content for msg in tool_messages] == ["done call_a", "done call_b"]


@pytest.mark.asyncio
async def test_execute_tools_deduplicates_repeated_read_calls_in_same_segment(tmp_path):
    graph = _graph(tmp_path)
    calls: list[dict] = []

    class FakeTools:
        async def execute_tool(self, tid, targs, _ctx):
            calls.append({"name": tid, "args": dict(targs)})
            return ToolResult(output=f"read {len(calls)}")

    async def allow_all(tool_calls, **_kwargs):
        return tool_calls, []

    graph.tools = FakeTools()
    graph._authorize_tool_calls = allow_all
    parent = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "read",
                "args": {"file_path": "src/voidx/workflow/reconcile.py"},
                "id": "call_read_1",
                "type": "tool_call",
            },
            {
                "name": "read",
                "args": {"file_path": "src/voidx/workflow/reconcile.py"},
                "id": "call_read_2",
                "type": "tool_call",
            },
            {
                "name": "read",
                "args": {"file_path": "src/voidx/workflow/reconcile.py"},
                "id": "call_read_3",
                "type": "tool_call",
            },
        ],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
    })

    assert calls == [
        {"name": "read", "args": {"file_path": "src/voidx/workflow/reconcile.py"}}
    ]
    tool_messages = [msg for msg in result["messages"] if isinstance(msg, ToolMessage)]
    assert [msg.tool_call_id for msg in tool_messages] == [
        "call_read_1",
        "call_read_2",
        "call_read_3",
    ]
    assert "read 1" == tool_messages[0].content
    assert "Skipped duplicate read" in tool_messages[1].content
    assert "call_read_1" in tool_messages[1].content
    assert "Skipped duplicate read" in tool_messages[2].content
    assert [msg.status for msg in tool_messages] == ["success", "success", "success"]
    assert "call_read_1" in tool_messages[2].content


@pytest.mark.asyncio
async def test_parallel_subagents_respects_max_concurrent(tmp_path):
    graph = VoidXGraph(
        Config(
            workspace=str(tmp_path),
            parallel_subagents=ParallelSubagentsConfig(enabled=True, max_concurrent=2),
        ),
        api_key=None,
    )
    active = 0
    max_active = 0

    class FakeAgentTool:
        id = "agent"
        description = "fake agent"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return ToolResult(output="done")

    graph.tools.register("agent", FakeAgentTool(), "fake agent", {"type": "object", "properties": {}})

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
            {"name": "agent", "args": {"description": "a"}, "id": "call_a", "type": "tool_call"},
            {"name": "agent", "args": {"description": "b"}, "id": "call_b", "type": "tool_call"},
            {"name": "agent", "args": {"description": "c"}, "id": "call_c", "type": "tool_call"},
        ],
    )

    await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
    })

    assert max_active == 2

