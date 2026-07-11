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
from voidx.ui.output.events import DockEventConsumer, StatusUpdated, ToolResultAppended, TurnStarted, ui_events


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
    from voidx.agent.graph.topology import route_after_execute_tools
    assert route_after_execute_tools(result) == "end"


@pytest.mark.asyncio
async def test_execute_tools_returns_tool_error_when_result_rendering_fails(tmp_path, monkeypatch):
    graph = _graph(tmp_path)

    class FakeReadTool:
        id = "read"
        description = "fake read"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            return ToolResult(output="File not found: missing.py", metadata={"error": True})

    class FailingResultConsumer:
        def handle(self, event):
            if isinstance(event, ToolResultAppended):
                raise RuntimeError("render exploded")
            return None

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

    graph._authorize_tool_calls = allow_all
    monkeypatch.setattr(graph._ui, "via_events", lambda: True)
    graph._ui.events.start(FailingResultConsumer())

    parent = AIMessage(
        content="",
        tool_calls=[{"name": "read", "args": {"file_path": "missing.py"}, "id": "call_read", "type": "tool_call"}],
    )

    try:
        result = await graph._execute_tools({
            "messages": [parent],
            "workspace": str(tmp_path),
            "persona": "voidx",
            "plan_mode": False,
        })
    finally:
        await graph._ui.events.stop()

    assert result["messages"][0].tool_call_id == "call_read"
    assert result["messages"][0].status == "error"
    assert result["messages"][0].content == "File not found: missing.py"


@pytest.mark.asyncio
async def test_execute_tools_emits_heartbeat_while_tool_is_still_running(tmp_path, monkeypatch):
    from voidx.agent.graph.tool_executor import executor as executor_module

    graph = _graph(tmp_path)
    emitted: list = []

    class SlowReadTool:
        id = "read"
        description = "slow read"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            await asyncio.sleep(0.03)
            return ToolResult(output="read output")

    graph.tools.register("read", SlowReadTool(), "slow read", {"type": "object", "properties": {}})

    async def allow_all(
        tool_calls,
        plan_mode: bool,
        session_id: str,
        interaction_mode=None,
        workflow_runs=None,
        runtime_persona=None,
    ):
        return tool_calls, []

    async def fake_request(event):
        emitted.append(event)
        return None

    async def fake_emit(event):
        emitted.append(event)
        return True

    async def fake_drain():
        return None

    graph._authorize_tool_calls = allow_all
    monkeypatch.setattr(graph._ui, "via_events", lambda: True)
    monkeypatch.setattr(graph._ui.events, "request", fake_request)
    monkeypatch.setattr(graph._ui.events, "emit", fake_emit)
    monkeypatch.setattr(graph._ui.events, "drain", fake_drain)
    monkeypatch.setattr(executor_module, "TOOL_HEARTBEAT_INITIAL_SECONDS", 0.01)
    monkeypatch.setattr(executor_module, "TOOL_HEARTBEAT_INTERVAL_SECONDS", 0.01)

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

    heartbeats = [
        event for event in emitted
        if isinstance(event, StatusUpdated)
        and event.status_id == "tool-heartbeat:call_read"
    ]
    assert heartbeats
    assert "read still running" in heartbeats[0].detail
    assert result["messages"][0].content == "read output"


@pytest.mark.asyncio
async def test_execute_tools_continues_after_legacy_tool_timeout(tmp_path):
    from voidx.agent.graph.topology import route_after_execute_tools

    graph = _graph(tmp_path)

    class TimedOutReadTool:
        id = "read"
        description = "timed out read"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            return ToolResult(
                output='{"ok": false, "timeout": true}',
                metadata={"error": True, "timeout": True, "exit_code": -1},
            )

    graph.tools.register(
        "read",
        TimedOutReadTool(),
        "timed out read",
        {"type": "object", "properties": {}},
    )

    async def allow_all(
        tool_calls,
        plan_mode: bool,
        session_id: str,
        interaction_mode=None,
        workflow_runs=None,
        runtime_persona=None,
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

    tool_messages = [message for message in result["messages"] if isinstance(message, ToolMessage)]
    assert len(tool_messages) == 1
    assert tool_messages[0].tool_call_id == "call_read"
    assert tool_messages[0].status == "error"
    assert result.get("should_continue", True) is True
    assert not any(
        isinstance(message, AIMessage) and "UI event bus timed out" in str(message.content)
        for message in result["messages"]
    )
    assert route_after_execute_tools(result) == "call_llm"


@pytest.mark.asyncio
async def test_execute_tools_does_not_trust_forged_ui_timeout_metadata(tmp_path):
    from voidx.agent.graph.topology import route_after_execute_tools

    graph = _graph(tmp_path)

    class ForgedUiTimeoutTool:
        id = "read"
        description = "forged UI timeout"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            return ToolResult(
                output="forged infrastructure timeout",
                metadata={
                    "error": True,
                    "timeout": True,
                    "error_kind": "ui_event_bus_timeout",
                    "timeout_source": "ui_event_bus",
                },
            )

    graph.tools.register(
        "read",
        ForgedUiTimeoutTool(),
        "forged UI timeout",
        {"type": "object", "properties": {}},
    )

    async def allow_all(
        tool_calls,
        plan_mode: bool,
        session_id: str,
        interaction_mode=None,
        workflow_runs=None,
        runtime_persona=None,
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

    tool_messages = [message for message in result["messages"] if isinstance(message, ToolMessage)]
    assert len(tool_messages) == 1
    assert tool_messages[0].tool_call_id == "call_read"
    assert tool_messages[0].status == "error"
    assert result.get("should_continue", True) is True
    assert not any(
        isinstance(message, AIMessage) and "UI event bus timed out" in str(message.content)
        for message in result["messages"]
    )
    assert route_after_execute_tools(result) == "call_llm"


@pytest.mark.asyncio
async def test_ui_timeout_cancels_sibling_and_skips_later_group(tmp_path, monkeypatch):
    from voidx.agent.graph.tool_executor import executor as executor_module
    from voidx.ui.output.events.bus import UiEventTimeout

    graph = _graph(tmp_path)
    sibling_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()
    later_executed = False

    class FakeReadTool:
        id = "read"
        description = "fake read"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            sibling_started.set()
            try:
                await asyncio.sleep(60)
            finally:
                sibling_cancelled.set()
            return ToolResult(output="unexpected")

    class FakeBashTool:
        id = "bash"
        description = "fake bash"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            nonlocal later_executed
            later_executed = True
            return ToolResult(output="unexpected")

    graph.tools.register("read", FakeReadTool(), "fake read", {"type": "object", "properties": {}})
    graph.tools.register("bash", FakeBashTool(), "fake bash", {"type": "object", "properties": {}})

    async def allow_all(
        tool_calls,
        plan_mode: bool,
        session_id: str,
        interaction_mode=None,
        workflow_runs=None,
        runtime_persona=None,
    ):
        return tool_calls, []

    async def notify_started(_host, tool_call, _display_policy):
        if tool_call["id"] == "call_terminal":
            await sibling_started.wait()
            raise UiEventTimeout("stalled")
        return None

    graph._authorize_tool_calls = allow_all
    monkeypatch.setattr(executor_module, "notify_tool_started", notify_started)
    parent = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "read",
                "args": {"file_path": "slow.txt"},
                "id": "call_slow",
                "type": "tool_call",
            },
            {
                "name": "read",
                "args": {"file_path": "terminal.txt"},
                "id": "call_terminal",
                "type": "tool_call",
            },
            {
                "name": "bash",
                "args": {"command": "echo later"},
                "id": "call_later",
                "type": "tool_call",
            },
        ],
    )

    result = await asyncio.wait_for(
        graph._execute_tools({
            "messages": [parent],
            "workspace": str(tmp_path),
            "persona": "voidx",
            "plan_mode": False,
        }),
        timeout=1,
    )

    tool_messages = [message for message in result["messages"] if isinstance(message, ToolMessage)]
    assert [message.tool_call_id for message in tool_messages] == [
        "call_slow",
        "call_terminal",
        "call_later",
    ]
    assert all(message.status == "error" for message in tool_messages)
    assert sibling_cancelled.is_set()
    assert later_executed is False
    assert result["should_continue"] is False
    from voidx.agent.graph.topology import route_after_execute_tools
    assert route_after_execute_tools(result) == "end"
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    assert not pending


@pytest.mark.asyncio
async def test_ui_timeout_skips_event_drain(tmp_path, monkeypatch):
    from voidx.agent.graph.tool_executor import executor as executor_module
    from voidx.ui.output.events.bus import UiEventTimeout

    graph = _graph(tmp_path)
    drain_called = False

    async def allow_all(
        tool_calls,
        plan_mode: bool,
        session_id: str,
        interaction_mode=None,
        workflow_runs=None,
        runtime_persona=None,
    ):
        return tool_calls, []

    async def notify_started(_host, _tool_call, _display_policy):
        raise UiEventTimeout("stalled")

    async def fake_drain():
        nonlocal drain_called
        drain_called = True

    graph._authorize_tool_calls = allow_all
    monkeypatch.setattr(graph._ui, "via_events", lambda: True)
    monkeypatch.setattr(graph._ui.events, "drain", fake_drain)
    monkeypatch.setattr(executor_module, "notify_tool_started", notify_started)

    parent = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "read",
                "args": {"file_path": "never-started.txt"},
                "id": "call_terminal",
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

    assert drain_called is False
    assert result["should_continue"] is False


@pytest.mark.asyncio
async def test_barrier_tool_timeout_blocks_suffix_but_continues_turn(tmp_path):
    from voidx.agent.graph.topology import route_after_execute_tools
    from voidx.tools.base import tool_timeout_metadata

    graph = _graph(tmp_path)
    suffix_executed = False

    class TimedOutCheckpointTool:
        id = "checkpoint"
        description = "timed out checkpoint"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            return ToolResult(
                output="Checkpoint timed out",
                metadata=tool_timeout_metadata("test"),
            )

    class SuffixReadTool:
        id = "read"
        description = "suffix read"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            nonlocal suffix_executed
            suffix_executed = True
            return ToolResult(output="unexpected")

    graph.tools.register(
        "checkpoint",
        TimedOutCheckpointTool(),
        "timed out checkpoint",
        {"type": "object", "properties": {}},
    )
    graph.tools.register(
        "read",
        SuffixReadTool(),
        "suffix read",
        {"type": "object", "properties": {}},
    )

    async def allow_all(
        tool_calls,
        plan_mode: bool,
        session_id: str,
        interaction_mode=None,
        workflow_runs=None,
        runtime_persona=None,
    ):
        return tool_calls, []

    graph._authorize_tool_calls = allow_all
    parent = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "checkpoint",
                "args": {},
                "id": "call_barrier",
                "type": "tool_call",
            },
            {
                "name": "read",
                "args": {"file_path": "after.txt"},
                "id": "call_suffix",
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

    tool_messages = [message for message in result["messages"] if isinstance(message, ToolMessage)]
    assert [message.tool_call_id for message in tool_messages] == ["call_barrier", "call_suffix"]
    assert tool_messages[0].status == "error"
    assert "Checkpoint timed out" in str(tool_messages[0].content)
    assert tool_messages[1].status == "error"
    assert "prior runtime barrier was failed" in str(tool_messages[1].content)
    assert suffix_executed is False
    assert result.get("should_continue", True) is True
    assert route_after_execute_tools(result) == "call_llm"


@pytest.mark.asyncio
async def test_ui_timeout_preserves_mixed_tool_result_order_and_reasons(tmp_path, monkeypatch):
    from voidx.agent.graph.runtime_guards import tool_call_key
    from voidx.agent.graph.tool_executor import executor as executor_module
    from voidx.ui.output.events.bus import UiEventTimeout

    graph = _graph(tmp_path)
    slow_started = asyncio.Event()
    slow_cancelled = asyncio.Event()
    executed_ids: list[str] = []

    class MixedReadTool:
        id = "read"
        description = "mixed read"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            path = args["file_path"]
            executed_ids.append(path)
            if path == "slow.txt":
                slow_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    slow_cancelled.set()
            return ToolResult(output=f"read:{path}")

    class LaterBashTool:
        id = "bash"
        description = "later bash"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            executed_ids.append("later-bash")
            return ToolResult(output="unexpected")

    graph.tools.register(
        "read",
        MixedReadTool(),
        "mixed read",
        {"type": "object", "properties": {}},
    )
    graph.tools.register(
        "bash",
        LaterBashTool(),
        "later bash",
        {"type": "object", "properties": {}},
    )

    calls = [
        {"name": "read", "args": {"file_path": "same.txt"}, "id": "call_read", "type": "tool_call"},
        {"name": "read", "args": {"file_path": "same.txt"}, "id": "call_duplicate", "type": "tool_call"},
        {"name": "read", "args": {"file_path": "blocked.txt"}, "id": "call_blocked", "type": "tool_call"},
        {"name": "read", "args": {"file_path": "denied.txt"}, "id": "call_denied", "type": "tool_call"},
        {"name": "read", "args": {"file_path": "slow.txt"}, "id": "call_slow", "type": "tool_call"},
        {"name": "read", "args": {"file_path": "terminal.txt"}, "id": "call_terminal", "type": "tool_call"},
        {"name": "bash", "args": {"command": "echo later"}, "id": "call_later", "type": "tool_call"},
    ]

    graph._runtime_guards = RuntimeGuardState()
    graph._runtime_guards.tool_failures.blocked_call_keys.add(tool_call_key(calls[2]))

    async def authorize(
        tool_calls,
        plan_mode: bool,
        session_id: str,
        interaction_mode=None,
        workflow_runs=None,
        runtime_persona=None,
    ):
        approved = [call for call in tool_calls if call["id"] != "call_denied"]
        denied = [
            (call, "Permission denied for mixed protocol regression.")
            for call in tool_calls
            if call["id"] == "call_denied"
        ]
        return approved, denied

    async def notify_started(_host, tool_call, _display_policy):
        if tool_call["id"] == "call_terminal":
            await slow_started.wait()
            raise UiEventTimeout("stalled")

    graph._authorize_tool_calls = authorize
    monkeypatch.setattr(executor_module, "notify_tool_started", notify_started)

    result = await asyncio.wait_for(
        graph._execute_tools({
            "messages": [AIMessage(content="", tool_calls=calls)],
            "workspace": str(tmp_path),
            "persona": "voidx",
            "plan_mode": False,
        }),
        timeout=1,
    )

    tool_messages = [message for message in result["messages"] if isinstance(message, ToolMessage)]
    assert [message.tool_call_id for message in tool_messages] == [call["id"] for call in calls]
    assert len({message.tool_call_id for message in tool_messages}) == len(calls)
    by_id = {message.tool_call_id: str(message.content) for message in tool_messages}
    assert by_id["call_read"] == "read:same.txt"
    assert "Skipped duplicate read" in by_id["call_duplicate"]
    assert "Runtime guard blocked" in by_id["call_blocked"]
    assert "Permission denied" in by_id["call_denied"]
    assert "was cancelled" in by_id["call_slow"]
    assert "Tool notification timed out" in by_id["call_terminal"]
    assert "was skipped" in by_id["call_later"]
    assert slow_cancelled.is_set()
    assert executed_ids == ["same.txt", "slow.txt"]
    assert result["should_continue"] is False
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    assert not pending


@pytest.mark.asyncio
async def test_infrastructure_results_do_not_pollute_runtime_guards():
    from voidx.agent.graph.tool_executor.guards import _record_runtime_guard_outcomes
    from voidx.agent.graph.tool_executor.types import _ExecutedTool, _tool_result_ok

    guard_state = RuntimeGuardState()
    host = SimpleNamespace(_pending_guidance=[])
    call = {"name": "read", "args": {"file_path": "same.txt"}, "id": "call_read"}
    executed = [
        _ExecutedTool(
            message=None,
            result=ToolResult(
                output="Tool notification timed out",
                metadata={
                    "error": True,
                    "timeout": True,
                    "error_kind": "ui_event_bus_timeout",
                    "timeout_source": "ui_event_bus",
                },
            ),
            tool_call=call,
            terminal_reason="ui_event_bus_timeout",
            runtime_guard_eligible=False,
        ),
        _ExecutedTool(
            message=None,
            result=ToolResult(
                output="read was cancelled",
                metadata={"error": True, "infrastructure_cancelled": True},
            ),
            tool_call=call,
            runtime_guard_eligible=False,
        ),
    ]

    decision = await _record_runtime_guard_outcomes(
        host,
        guard_state,
        executed,
        previous_todo_state=None,
        next_todo_state=None,
        workflow_changed=False,
        result_ok=_tool_result_ok,
    )

    assert decision.action == "allow"
    assert guard_state.tool_failures.count == 0
    assert guard_state.tool_failures.should_block(call) is False
    assert guard_state.repetitive_tools.recent_cycles == []
    assert guard_state.no_progress.consecutive == 0
    assert host._pending_guidance == []


@pytest.mark.asyncio
async def test_ordinary_tool_timeout_remains_runtime_guard_eligible():
    from voidx.agent.graph.tool_executor.guards import _record_runtime_guard_outcomes
    from voidx.agent.graph.tool_executor.types import _ExecutedTool, _tool_result_ok
    from voidx.tools.base import tool_timeout_metadata

    guard_state = RuntimeGuardState()
    host = SimpleNamespace(_pending_guidance=[])
    call = {"name": "read", "args": {"file_path": "slow.txt"}, "id": "call_read"}
    executed = [
        _ExecutedTool(
            message=None,
            result=ToolResult(
                output="read timed out",
                metadata=tool_timeout_metadata("test"),
            ),
            tool_call=call,
        ),
    ]

    await _record_runtime_guard_outcomes(
        host,
        guard_state,
        executed,
        previous_todo_state=None,
        next_todo_state=None,
        workflow_changed=False,
        result_ok=_tool_result_ok,
    )

    assert guard_state.tool_failures.count == 1
    assert guard_state.no_progress.consecutive == 1
    assert len(guard_state.repetitive_tools.recent_cycles) == 1


@pytest.mark.asyncio
async def test_execute_approved_batch_cancels_pending_tasks_on_outer_cancellation():
    from voidx.agent.graph.tool_executor.helpers import _execute_approved_batch
    from voidx.agent.graph.tool_executor.types import _ExecutedTool

    started: dict[str, asyncio.Event] = {
        "call_a": asyncio.Event(),
        "call_b": asyncio.Event(),
    }
    cancelled: dict[str, asyncio.Event] = {
        "call_a": asyncio.Event(),
        "call_b": asyncio.Event(),
    }

    async def execute_one(tool_call: dict) -> _ExecutedTool:
        call_id = tool_call["id"]
        started[call_id].set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled[call_id].set()

    approved = [
        {"name": "bash", "args": {"command": "sleep 10"}, "id": "call_a", "type": "tool_call"},
        {"name": "bash", "args": {"command": "sleep 10"}, "id": "call_b", "type": "tool_call"},
    ]
    host = SimpleNamespace(
        config=Config(workspace="."),
        _ui=SimpleNamespace(via_events=lambda: False),
    )

    task = asyncio.create_task(_execute_approved_batch(
        approved,
        host=host,
        guard_state=RuntimeGuardState(),
        execute_one_fn=execute_one,
    ))
    await asyncio.wait_for(started["call_a"].wait(), timeout=1)
    await asyncio.wait_for(started["call_b"].wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cancelled["call_a"].is_set()
    assert cancelled["call_b"].is_set()


@pytest.mark.asyncio
async def test_real_event_bus_stall_terminates_turn_without_drain(tmp_path, monkeypatch):
    """Regression: a real blocked UiEventBus consumer must cause a bounded turn
    termination that skips drain and leaves no pending task."""
    from voidx.agent.graph.tool_executor import executor as executor_module
    from voidx.agent.graph.topology import route_after_execute_tools
    from voidx.ui.output.events.bus import UiEventBus
    from voidx.ui.output.events.schema import ToolStarted

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

    graph._authorize_tool_calls = allow_all

    bus = UiEventBus()
    consumer_block = asyncio.Event()

    class BlockedConsumer:
        async def handle(self, event):
            if isinstance(event, ToolStarted):
                await consumer_block.wait()
            return None

    bus.start(BlockedConsumer())

    original_request = bus.request

    async def fast_request(event, *, timeout: float = 5.0, max_retries: int = 10):
        return await original_request(event, timeout=0.05, max_retries=1)

    drain_called = False
    original_drain = bus.drain

    async def tracking_drain():
        nonlocal drain_called
        drain_called = True
        await original_drain()

    bus.drain = tracking_drain

    original_ui = graph._ui
    fake_ui = SimpleNamespace(
        via_events=lambda: True,
        events=bus,
        dock=original_ui.dock,
        ui=original_ui.ui,
        session_tracker=original_ui.session_tracker,
    )
    graph._ui = fake_ui

    async def real_notify(host, tc, dp):
        return await fast_request(ToolStarted(
            tool_call_id=tc.get("id", ""),
            tool_name=tc.get("name", ""),
            label=tc.get("name", ""),
            args="",
            raw_args=tc.get("args", {}),
            display_mode="show",
            summary_max_lines=5,
        ))

    monkeypatch.setattr(executor_module, "notify_tool_started", real_notify)

    parent = AIMessage(
        content="",
        tool_calls=[{"name": "read", "args": {}, "id": "call_read", "type": "tool_call"}],
    )

    try:
        result = await asyncio.wait_for(
            graph._execute_tools({
                "messages": [parent],
                "workspace": str(tmp_path),
                "persona": "voidx",
                "plan_mode": False,
            }),
            timeout=3,
        )
    finally:
        consumer_block.set()
        await bus.stop()

    assert result["should_continue"] is False
    assert drain_called is False
    assert route_after_execute_tools(result) == "end"
    assert any(
        isinstance(message, ToolMessage)
        and message.tool_call_id == "call_read"
        and message.status == "error"
        for message in result["messages"]
    )
    assert any(
        isinstance(message, AIMessage) and "UI event bus timed out" in str(message.content)
        for message in result["messages"]
    )
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    assert not pending


@pytest.mark.asyncio
async def test_prefix_terminal_blocks_barrier_and_suffix(tmp_path, monkeypatch):
    """A terminal UI timeout in the prefix group must prevent barrier and suffix
    from executing, and route to end."""
    from voidx.agent.graph.tool_executor import executor as executor_module
    from voidx.agent.graph.topology import route_after_execute_tools
    from voidx.ui.output.events.bus import UiEventTimeout

    graph = _graph(tmp_path)
    barrier_executed = False
    suffix_executed = False

    class FakeReadTool:
        id = "read"
        description = "fake read"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            return ToolResult(output="read output")

    class FakeCheckpointTool:
        id = "checkpoint"
        description = "fake checkpoint"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            nonlocal barrier_executed
            barrier_executed = True
            return ToolResult(output="unexpected")

    class FakeBashTool:
        id = "bash"
        description = "fake bash"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            nonlocal suffix_executed
            suffix_executed = True
            return ToolResult(output="unexpected")

    graph.tools.register("read", FakeReadTool(), "fake read", {"type": "object", "properties": {}})
    graph.tools.register("checkpoint", FakeCheckpointTool(), "fake checkpoint", {"type": "object", "properties": {}})
    graph.tools.register("bash", FakeBashTool(), "fake bash", {"type": "object", "properties": {}})

    async def allow_all(
        tool_calls,
        plan_mode: bool,
        session_id: str,
        interaction_mode=None,
        workflow_runs=None,
        runtime_persona=None,
    ):
        return tool_calls, []

    async def notify_started(_host, tool_call, _display_policy):
        if tool_call["id"] == "call_prefix":
            raise UiEventTimeout("stalled")
        return None

    graph._authorize_tool_calls = allow_all
    monkeypatch.setattr(executor_module, "notify_tool_started", notify_started)

    parent = AIMessage(
        content="",
        tool_calls=[
            {"name": "read", "args": {"file_path": "prefix.txt"}, "id": "call_prefix", "type": "tool_call"},
            {"name": "checkpoint", "args": {}, "id": "call_barrier", "type": "tool_call"},
            {"name": "bash", "args": {"command": "echo suffix"}, "id": "call_suffix", "type": "tool_call"},
        ],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
    })

    tool_messages = [message for message in result["messages"] if isinstance(message, ToolMessage)]
    assert [message.tool_call_id for message in tool_messages] == [
        "call_prefix",
        "call_barrier",
        "call_suffix",
    ]
    assert all(message.status == "error" for message in tool_messages)
    assert barrier_executed is False
    assert suffix_executed is False
    assert result["should_continue"] is False
    assert route_after_execute_tools(result) == "end"
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    assert not pending


@pytest.mark.asyncio
async def test_barrier_terminal_blocks_suffix(tmp_path, monkeypatch):
    """A terminal UI timeout for the barrier tool must prevent its suffix from
    executing, and route to end."""
    from voidx.agent.graph.tool_executor import executor as executor_module
    from voidx.agent.graph.topology import route_after_execute_tools
    from voidx.ui.output.events.bus import UiEventTimeout

    graph = _graph(tmp_path)
    suffix_executed = False

    class FakeCheckpointTool:
        id = "checkpoint"
        description = "fake checkpoint"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            return ToolResult(output="should not execute")

    class FakeBashTool:
        id = "bash"
        description = "fake bash"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            nonlocal suffix_executed
            suffix_executed = True
            return ToolResult(output="unexpected")

    graph.tools.register("checkpoint", FakeCheckpointTool(), "fake checkpoint", {"type": "object", "properties": {}})
    graph.tools.register("bash", FakeBashTool(), "fake bash", {"type": "object", "properties": {}})

    async def allow_all(
        tool_calls,
        plan_mode: bool,
        session_id: str,
        interaction_mode=None,
        workflow_runs=None,
        runtime_persona=None,
    ):
        return tool_calls, []

    async def notify_started(_host, tool_call, _display_policy):
        if tool_call["id"] == "call_barrier":
            raise UiEventTimeout("stalled")
        return None

    graph._authorize_tool_calls = allow_all
    monkeypatch.setattr(executor_module, "notify_tool_started", notify_started)

    parent = AIMessage(
        content="",
        tool_calls=[
            {"name": "checkpoint", "args": {}, "id": "call_barrier", "type": "tool_call"},
            {"name": "bash", "args": {"command": "echo suffix"}, "id": "call_suffix", "type": "tool_call"},
        ],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
    })

    tool_messages = [message for message in result["messages"] if isinstance(message, ToolMessage)]
    assert [message.tool_call_id for message in tool_messages] == ["call_barrier", "call_suffix"]
    assert all(message.status == "error" for message in tool_messages)
    assert "Tool notification timed out" in str(tool_messages[0].content)
    assert "was skipped" in str(tool_messages[1].content)
    assert suffix_executed is False
    assert result["should_continue"] is False
    assert route_after_execute_tools(result) == "end"
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    assert not pending


@pytest.mark.asyncio
async def test_ui_timeout_no_inherited_block_after_recovery(tmp_path, monkeypatch):
    """After a UI event bus timeout, the same tool call must execute without
    inheriting a repeated-failure block from the infrastructure event."""
    from voidx.agent.graph.tool_executor import executor as executor_module
    from voidx.ui.output.events.bus import UiEventTimeout

    graph = _graph(tmp_path)

    class FakeReadTool:
        id = "read"
        description = "fake read"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            return ToolResult(output="read output")

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

    call_count = 0

    async def notify_started(_host, _tool_call, _display_policy):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise UiEventTimeout("stalled")
        return None

    graph._authorize_tool_calls = allow_all
    monkeypatch.setattr(executor_module, "notify_tool_started", notify_started)

    parent = AIMessage(
        content="",
        tool_calls=[{"name": "read", "args": {"file_path": "test.txt"}, "id": "call_read", "type": "tool_call"}],
    )

    result1 = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
    })

    assert result1["should_continue"] is False

    result2 = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
    })

    assert result2.get("should_continue", True) is True
    tool_messages = [message for message in result2["messages"] if isinstance(message, ToolMessage)]
    assert len(tool_messages) == 1
    assert tool_messages[0].tool_call_id == "call_read"
    assert tool_messages[0].status != "error"
    assert "read output" in str(tool_messages[0].content)
