"""Regression tests for core graph behavior."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace


import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

import voidx.persistence.sqlite as store

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
from tests.langgraph_execution import make_langgraph_execution
from voidx.agent.infrastructure.langgraph.execution import AGENT_RESULT_PREVIEW_CHARS, _agent_result_preview
from voidx.agent.infrastructure.message_rows import RowMessageCacheEntry
from voidx.agent.application.runtime_context import InteractionMode, RuntimeContextBuilder
from voidx.config import Config, Settings
from voidx.agent.domain.user_profile import UserProfile
from voidx.llm.compaction import CompactionSelection
from voidx.agent.application.instruction import InstructionService, WorkflowRuntimeContext
from voidx.agent.adapters.persistence.session_repository import (
    MessageRow,
    SessionInfo,
    create_session,
    delete_session,
    load_messages,
    save_message,
)
from voidx.presentation.adapters.persistence.transcript_snapshot import load_transcript
from voidx.tooling.adapters.permission.in_memory_state import create_permission_service as PermissionService
from voidx.agent.domain.task.state import GoalResolution, GoalSpec, IntentResolution, PlanResolution
from voidx.agent.domain.task.intent import TaskIntent
from voidx.skills.context import SKILL_TOOL_CONTEXT_MARKER
from voidx.agent.application.automation.workflow.context import WORKFLOW_CONTEXT_MARKER
from voidx.agent.application.automation.workflow.runtime import WorkflowRunState, WorkflowRunStatus
from voidx.agent.domain.task.state import TaskState, ToolStatePatch
from voidx.agent.domain.automation.workflow import WorkflowRoute
from voidx.tooling.domain.context import ToolExecutionContext as ToolContext
from voidx.tooling.domain.result import ToolResult
from voidx.agent.adapters.tools.subagent import AgentResultContract, AgentTool
from voidx.tooling.application.registry import ToolRegistry
from voidx.presentation.output.dock import BottomInputDock, set_dock
from voidx.presentation.output.events import DockEventConsumer, TurnStarted, ui_events
from voidx.agent.domain.automation.loop import LOOP_PROFILE, LoopToolView
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.infrastructure.langgraph.runtime.thread_context import (
    ThreadExecutionState,
    _CURRENT_THREAD_EXECUTION_STATE,
)
from tests.test_agent.adapters.tools.test_loop import FakeLoopController


def _graph(tmp_path):
    cfg = Config(workspace=str(tmp_path))
    return make_langgraph_execution(cfg, api_key=None)


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
async def test_execute_tools_does_not_inject_parallel_child_agent_buffers(tmp_path):
    graph = make_langgraph_execution(
        Config(
            workspace=str(tmp_path),
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
                await asyncio.sleep(0.01)
            return ToolResult(output=f"done {call_id}")

    graph.tools.replace("agent", FakeAgentTool(), "fake agent", {"type": "object", "properties": {}})

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
    assert all(isinstance(msg, ToolMessage) for msg in messages)
    assert [msg.tool_call_id for msg in messages] == ["call_a", "call_b"]
    assert [msg.content for msg in messages] == ["done call_a", "done call_b"]


@pytest.mark.asyncio
async def test_execute_tools_emits_todo_updated_node(tmp_path):
    graph = _graph(tmp_path)

    class FakeTodoTool:
        id = "todo"
        description = "fake todo"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            return ToolResult(
                title="Todo",
                output="todo output",
                metadata={
                    "todo_summary": "0/1 done · 1 active · 0 pending",
                    "todo_items": [{"id": "wire", "content": "wire event", "status": "active"}],
                    "total": 1, "done": 0, "active": 1, "pending": 0,
                },
            )

    graph.tools.replace("todo", FakeTodoTool(), "fake todo", {"type": "object", "properties": {}})

    async def allow_all(
        tool_calls,
        plan_mode: bool,
        session_id: str,
        interaction_mode=None,
    ):
        return tool_calls, []

    graph._authorize_tool_calls = allow_all
    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    ui_events.start(DockEventConsumer(test_dock))
    try:
        await ui_events.request(TurnStarted(text="demo"))
        parent = AIMessage(
            content="",
            tool_calls=[{
                "name": "todo",
                "args": {"todos": []},
                "id": "call_todo",
                "type": "tool_call",
            }],
        )

        result = await graph._execute_tools({
            "messages": [parent],
            "workspace": str(tmp_path),
            "persona": "voidx",
            "plan_mode": False,
        })
        await ui_events.drain()

        todo_state = test_dock.todo_state()
        tool_nodes = [
            node
            for node in _tree_nodes(test_dock.tree.root)
            if node.node_type in {"tool_call", "tool_result"}
        ]

        assert todo_state is not None
        assert [(item.id, item.content, item.status) for item in todo_state.items] == [("wire", "wire event", "active")]
        assert todo_state.summary == "0/1 done · 1 active · 0 pending"
        assert not any(node.node_type == "todo" for node in test_dock.tree.root.children)
        assert tool_nodes == []
        assert [message.tool_call_id for message in result["messages"]] == ["call_todo"]
        assert result["messages"][0].content == "todo output"
        assert result["todo_state"]["summary"] == "0/1 done · 1 active · 0 pending"
        assert result["todo_state"]["active_items"] == [{"id": "wire", "content": "wire event", "status": "active"}]
        assert graph._task_state.todo_state is not None
        assert graph._task_state.todo_state.active_items[0].content == "wire event"
    finally:
        await ui_events.stop()
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)


@pytest.mark.parametrize("tool_name", ["clarify", "checkpoint"])
@pytest.mark.asyncio
async def test_execute_tools_keeps_hidden_tool_failures_out_of_ui(tmp_path, tool_name):
    graph = _graph(tmp_path)

    class FakeHiddenTool:
        id = tool_name
        description = f"fake {tool_name}"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            return ToolResult(output=f"{tool_name} failed internally", metadata={"error": True})

    graph.tools.replace(tool_name, FakeHiddenTool(), f"fake {tool_name}", {"type": "object", "properties": {}})

    async def allow_all(
        tool_calls,
        plan_mode: bool,
        session_id: str,
        interaction_mode=None,
    ):
        return tool_calls, []

    graph._authorize_tool_calls = allow_all
    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    ui_events.start(DockEventConsumer(test_dock))
    try:
        await ui_events.request(TurnStarted(text="demo"))
        parent = AIMessage(
            content="",
            tool_calls=[{
                "name": tool_name,
                "args": {},
                "id": f"call_{tool_name}",
                "type": "tool_call",
            }],
        )

        result = await graph._execute_tools({
            "messages": [parent],
            "workspace": str(tmp_path),
            "persona": "voidx",
            "plan_mode": False,
        })
        await ui_events.drain()

        visible_nodes = [
            node
            for node in _tree_nodes(test_dock.tree.root)
            if node.node_type in {"warn", "tool_call", "tool_result"}
        ]

        assert visible_nodes == []
        assert [message.tool_call_id for message in result["messages"]] == [f"call_{tool_name}"]
        assert result["messages"][0].status == "error"
        assert result["messages"][0].content == f"{tool_name} failed internally"
    finally:
        await ui_events.stop()
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)


@pytest.mark.asyncio
async def test_execute_tools_warns_then_skips_repeated_todo_without_progress(tmp_path):
    graph = _graph(tmp_path)
    calls = 0

    class FakeTodoTool:
        id = "todo"
        description = "fake todo"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            nonlocal calls
            calls += 1
            return ToolResult(
                output="todo output",
                metadata={
                    "todo_summary": "0/1 done · 0 active · 1 pending",
                    "todo_items": [{"id": "task", "content": "same task", "status": "pending"}],
                    "total": 1, "done": 0, "active": 0, "pending": 1,
                },
            )

    async def allow_all(tool_calls, **_kwargs):
        return tool_calls, []

    graph.tools.replace("todo", FakeTodoTool(), "fake todo", {"type": "object", "properties": {}})
    graph._authorize_tool_calls = allow_all

    async def run_todo(call_id: str):
        parent = AIMessage(
            content="",
            tool_calls=[{"name": "todo", "args": {"todos": []}, "id": call_id, "type": "tool_call"}],
        )
        return await graph._execute_tools({
            "messages": [parent],
            "workspace": str(tmp_path),
            "persona": "voidx",
            "plan_mode": False,
        })

    await run_todo("call_todo_1")
    await run_todo("call_todo_2")
    assert graph._pending_guidance == []

    await run_todo("call_todo_3")
    assert any("only called todo:read" in item[0] for item in graph._pending_guidance)

    result = await run_todo("call_todo_4")
    assert calls == 3
    assert result["messages"][0].tool_call_id == "call_todo_4"
    assert "Runtime guard skipped repeated todo:read call" in result["messages"][0].content
    assert result.get("should_continue", True) is True


@pytest.mark.asyncio
async def test_execute_loop_protocol_tool_without_registry_tool(tmp_path):
    graph = _graph(tmp_path)
    controller = FakeLoopController()
    tool_policy = LoopToolView.default(workflow_enabled=False).bind({"loop", *graph.tools.ids()})
    turn_context = TurnExecutionContext(
        thread_id="loop:test:gen",
        session_id="loop:test:gen",
        runtime_profile=LOOP_PROFILE,
        workspace=str(tmp_path),
        tool_policy=tool_policy,
        loop_controller=controller,
    )
    thread_state = ThreadExecutionState(
        thread_id="loop:test:gen",
        turn_context=turn_context,
        runtime_profile=LOOP_PROFILE,
        tool_policy=tool_policy,
        workspace=str(tmp_path),
    )
    parent = AIMessage(
        content="",
        tool_calls=[{
            "name": "loop",
            "args": {"operation": "commit", "outcome": "continue", "summary": "checked"},
            "id": "call_loop_1",
            "type": "tool_call",
        }],
    )

    token = _CURRENT_THREAD_EXECUTION_STATE.set(thread_state)
    try:
        result = await graph._execute_tools({
            "messages": [parent],
            "workspace": str(tmp_path),
            "persona": "voidx",
            "plan_mode": False,
        })
    finally:
        _CURRENT_THREAD_EXECUTION_STATE.reset(token)

    assert "loop" in graph.tools.ids()
    assert controller.decisions[0].outcome == "continue"
    assert result["should_continue"] is False
    assert result["messages"][0].status == "success"


@pytest.mark.asyncio
async def test_execute_tools_no_progress_guidance_and_termination(tmp_path):
    graph = _graph(tmp_path)
    calls: list[str] = []

    class FakeTools:
        async def execute_tool(self, tid, _targs, _ctx):
            calls.append(tid)
            return ToolResult(output=f"{tid} ok")

    async def allow_all(tool_calls, **_kwargs):
        return tool_calls, []

    graph.tools = FakeTools()
    graph._authorize_tool_calls = allow_all

    async def run_tool(tool_name: str, call_id: str):
        parent = AIMessage(
            content="",
            tool_calls=[{"name": tool_name, "args": {}, "id": call_id, "type": "tool_call"}],
        )
        return await graph._execute_tools({
            "messages": [parent],
            "workspace": str(tmp_path),
            "persona": "voidx",
            "plan_mode": False,
        })

    # Run enough checkpoint cycles to exhaust repetitive guard skip then no_progress terminate.
    result = None
    for i in range(1, 7):
        result = await run_tool("checkpoint", f"call_{i}")
        if result.get("should_continue") is False:
            break

    # termination may come from repetitive guard or no_progress guard
    assert result is not None
    assert result["should_continue"] is False
    has_termination_msg = any(
        isinstance(m, AIMessage)
        and ("stopped this turn" in str(m.content) or "stopped this turn" in str(m.content).lower())
        for m in result.get("messages", [])
    )
    assert has_termination_msg

