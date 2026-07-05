"""Regression tests for core graph behavior."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

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
async def test_execute_tools_keeps_non_todo_result_in_mixed_batch(tmp_path):
    graph = _graph(tmp_path)

    class FakeTodoTool:
        id = "todo"
        description = "fake todo"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            return ToolResult(
                output="todo output",
                metadata={
                    "todo_summary": "0/1 done · 1 active · 0 pending",
                    "todo_items": [{"id": "mixed", "content": "track mixed batch", "status": "active"}],
                    "total": 1, "done": 0, "active": 1, "pending": 0,
                },
            )

    class FakeReadTool:
        id = "read"
        description = "fake read"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            return ToolResult(output="read output")

    graph.tools.register("todo", FakeTodoTool(), "fake todo", {"type": "object", "properties": {}})
    graph.tools.register("read", FakeReadTool(), "fake read", {"type": "object", "properties": {}})

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
            {"name": "todo", "args": {"todos": []}, "id": "call_todo", "type": "tool_call"},
            {"name": "read", "args": {}, "id": "call_read", "type": "tool_call"},
        ],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
    })

    assert [message.tool_call_id for message in result["messages"]] == ["call_todo", "call_read"]
    assert [message.content for message in result["messages"]] == ["todo output", "read output"]
    assert result["todo_state"]["active_items"] == [
        {"id": "mixed", "content": "track mixed batch", "status": "active"}
    ]


@pytest.mark.asyncio
async def test_execute_tools_warns_on_malformed_todo_metadata_without_events(tmp_path, monkeypatch):
    graph = _graph(tmp_path)
    warnings: list[str] = []

    class FakeTodoTool:
        id = "todo"
        description = "fake todo"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            return ToolResult(output="todo output", metadata={"todo_items": "bad"})

    graph.tools.register("todo", FakeTodoTool(), "fake todo", {"type": "object", "properties": {}})
    monkeypatch.setattr(graph._ui.ui, "warn", warnings.append)

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
        tool_calls=[{"name": "todo", "args": {"todos": []}, "id": "call_todo", "type": "tool_call"}],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
    })

    assert [message.tool_call_id for message in result["messages"]] == ["call_todo"]
    assert result["messages"][0].content == "todo output"
    assert "todo_state" not in result
    assert warnings == ["Todo update ignored: tool returned malformed metadata."]



@pytest.mark.asyncio
async def test_execute_tools_read_todo_no_warning_without_events(tmp_path, monkeypatch):
    """op=read should not trigger 'malformed metadata' warning (non-events path)."""
    graph = _graph(tmp_path)
    warnings: list[str] = []

    class FakeTodoTool:
        id = "todo"
        description = "fake todo"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            return ToolResult(
                output="Todo list is empty.",
                metadata={"todo_op": "read"},
            )

    graph.tools.register("todo", FakeTodoTool(), "fake todo", {"type": "object", "properties": {}})
    monkeypatch.setattr(graph._ui.ui, "warn", warnings.append)

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
        tool_calls=[{"name": "todo", "args": {"op": "read"}, "id": "call_todo", "type": "tool_call"}],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
    })

    assert [message.tool_call_id for message in result["messages"]] == ["call_todo"]
    assert result["messages"][0].content == "Todo list is empty."
    assert "todo_state" not in result
    assert warnings == []


@pytest.mark.asyncio
async def test_execute_tools_read_todo_no_warning_with_events(tmp_path, monkeypatch):
    """op=read should not emit WarningAppended in events mode."""
    graph = _graph(tmp_path)
    emitted: list = []

    class FakeTodoTool:
        id = "todo"
        description = "fake todo"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            return ToolResult(
                output="Todo list is empty.",
                metadata={"todo_op": "read"},
            )

    graph.tools.register("todo", FakeTodoTool(), "fake todo", {"type": "object", "properties": {}})
    monkeypatch.setattr(graph._ui, "via_events", lambda: True)

    async def fake_emit(event):
        emitted.append(event)

    async def fake_request(event):
        return None

    monkeypatch.setattr(graph._ui.events, "emit", fake_emit)
    monkeypatch.setattr(graph._ui.events, "request", fake_request)

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
        tool_calls=[{"name": "todo", "args": {"op": "read"}, "id": "call_todo", "type": "tool_call"}],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
    })

    assert [message.tool_call_id for message in result["messages"]] == ["call_todo"]
    assert "todo_state" not in result
    from voidx.runtime.ui import WarningAppended
    assert not any(isinstance(e, WarningAppended) for e in emitted)

@pytest.mark.asyncio
async def test_subagent_full_output_reaches_orchestrator(tmp_path, monkeypatch):
    import voidx.agent.graph.subagent as subagent_module

    graph = _graph(tmp_path)
    child_output = "\n".join(f"child final line {index}" for index in range(1, 8))

    class FakeSubagentModel:
        def bind_tools(self, _tool_defs):
            return self

        async def astream(self, _messages):
            yield AIMessageChunk(content=[{"type": "thinking", "text": "child hidden thought"}])
            yield AIMessageChunk(content=child_output)

    monkeypatch.setattr(
        subagent_module,
        "create_chat_model",
        lambda *_args, **_kwargs: FakeSubagentModel(),
    )

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
        graph._current_tree = test_dock.tree
        graph._turn_node = await ui_events.request(TurnStarted(text="demo"))
        parent = AIMessage(
            content="",
                tool_calls=[{
                    "name": "agent",
                    "args": {
                        "agent": "voidx",
                        "mode": "inspect",
                        "task": "Inspect the auth flow",
                        "target": "src/voidx/auth.py",
                        "result_preset": "inspection",
                    },
                    "id": "call_agent",
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

        assistant = next(node for node in test_dock.tree.root.children if node.node_type == "assistant")
        subagent = next(node for node in assistant.children if node.node_type == "subagent")
        child_streams = [
            node for node in subagent.children
            if node.node_type == "assistant" and "child final line" in node.header
        ]

        rendered = "\n".join(test_dock.tree.render(120))
        assert child_streams == []
        assert "child hidden thought" not in rendered
        assert "child final line 7" not in rendered
        tool_messages = [message for message in result["messages"] if isinstance(message, ToolMessage)]
        assert tool_messages[0].tool_call_id == "call_agent"
        assert tool_messages[0].content == child_output
        assert not any(isinstance(message, AIMessage) and message.content == child_output for message in result["messages"])
    finally:
        await ui_events.stop()
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)
        graph._current_tree = None
        graph._turn_node = None




@pytest.mark.asyncio
async def test_execute_tools_wraps_write_risk_tool_with_workspace_write_lock(tmp_path):
    graph = _graph(tmp_path)
    graph._session = SimpleNamespace(id="thread-write")
    events: list[tuple[str, str]] = []

    class FakeRunManager:
        async def acquire_workspace_write_lock(self, thread_id: str) -> bool:
            events.append(("acquire", thread_id))
            return True

        def release_workspace_write_lock(self, thread_id: str) -> None:
            events.append(("release", thread_id))

    graph._gateway_session = SimpleNamespace(_run_manager=FakeRunManager())

    class FakeWriteTool:
        id = "write"
        description = "fake write"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            events.append(("execute", ctx.session_id))
            return ToolResult(output="write output")

    graph.tools.register("write", FakeWriteTool(), "fake write", {"type": "object", "properties": {}})

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
        tool_calls=[{"name": "write", "args": {}, "id": "call_write", "type": "tool_call"}],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
    })

    assert [message.tool_call_id for message in result["messages"]] == ["call_write"]
    assert result["messages"][0].content == "write output"
    assert events == [
        ("acquire", "thread-write"),
        ("execute", "thread-write"),
        ("release", "thread-write"),
    ]


@pytest.mark.asyncio
async def test_execute_tools_releases_workspace_write_lock_on_tool_exception(tmp_path):
    graph = _graph(tmp_path)
    graph._session = SimpleNamespace(id="thread-write")
    events: list[tuple[str, str]] = []

    class FakeRunManager:
        async def acquire_workspace_write_lock(self, thread_id: str) -> bool:
            events.append(("acquire", thread_id))
            return True

        def release_workspace_write_lock(self, thread_id: str) -> None:
            events.append(("release", thread_id))

    graph._gateway_session = SimpleNamespace(_run_manager=FakeRunManager())

    class FailingWriteTool:
        id = "write"
        description = "failing write"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            events.append(("execute", ctx.session_id))
            raise RuntimeError("boom")

    graph.tools.register("write", FailingWriteTool(), "failing write", {"type": "object", "properties": {}})

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
        tool_calls=[{"name": "write", "args": {}, "id": "call_write", "type": "tool_call"}],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
    })

    assert result["messages"][0].tool_call_id == "call_write"
    assert result["messages"][0].status == "error"
    assert events == [
        ("acquire", "thread-write"),
        ("execute", "thread-write"),
        ("release", "thread-write"),
    ]


@pytest.mark.asyncio
async def test_execute_tools_does_not_lock_read_only_tool(tmp_path):
    graph = _graph(tmp_path)
    graph._session = SimpleNamespace(id="thread-read")
    events: list[tuple[str, str]] = []

    class FakeRunManager:
        async def acquire_workspace_write_lock(self, thread_id: str) -> bool:
            events.append(("acquire", thread_id))
            return True

        def release_workspace_write_lock(self, thread_id: str) -> None:
            events.append(("release", thread_id))

    graph._gateway_session = SimpleNamespace(_run_manager=FakeRunManager())

    class FakeReadTool:
        id = "read"
        description = "fake read"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            events.append(("execute", ctx.session_id))
            return ToolResult(output="read output")

    graph.tools.register("read", FakeReadTool(), "fake read", {"type": "object", "properties": {}})

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
        tool_calls=[{"name": "read", "args": {}, "id": "call_read", "type": "tool_call"}],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
    })

    assert [message.tool_call_id for message in result["messages"]] == ["call_read"]
    assert result["messages"][0].content == "read output"
    assert events == [("execute", "thread-read")]


@pytest.mark.asyncio
async def test_execute_tools_terminates_turn_when_tool_started_notification_times_out(tmp_path, monkeypatch):
    from voidx.agent.graph.tool_executor import executor as executor_module
    from voidx.ui.output.events.bus import UiEventTimeout

    graph = _graph(tmp_path)

    class FakeReadTool:
        id = "read"
        description = "fake read"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            return ToolResult(output="should not execute")

    graph.tools.register("read", FakeReadTool(), "fake read", {"type": "object", "properties": {}})

    async def allow_all(
        tool_calls,
        plan_mode: bool,
        session_id: str,
        interaction_mode=None,
        workflow_runs=None,
        runtime_persona=None,
    ):
        return tool_calls, []

    async def timeout_tool_started(*_args, **_kwargs):
        raise UiEventTimeout("stalled")

    graph._authorize_tool_calls = allow_all
    monkeypatch.setattr(executor_module, "notify_tool_started", timeout_tool_started)

    parent = AIMessage(
        content="",
        tool_calls=[{"name": "read", "args": {}, "id": "call_read", "type": "tool_call"}],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
    })

    assert result["should_continue"] is False
    assert any(
        isinstance(message, ToolMessage)
        and message.tool_call_id == "call_read"
        and message.status == "error"
        and "Tool notification timed out" in message.content
        for message in result["messages"]
    )
    assert any(
        isinstance(message, AIMessage)
        and "UI event bus timed out" in str(message.content)
        for message in result["messages"]
    )
