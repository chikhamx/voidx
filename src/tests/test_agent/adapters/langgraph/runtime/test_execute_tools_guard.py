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
)
from voidx.agent.application.prompts import BASE_SYSTEM, PERSONA_MODEL, persona_prompt
from voidx.agent.adapters.langgraph.runtime.convergence import is_step_hint_message
from voidx.agent.adapters.langgraph.runtime.runtime import current_parent_tool_call_id
from voidx.agent.adapters.langgraph.runtime.runtime_guards import RuntimeGuardState, WallClockGuardState
from voidx.agent.adapters.langgraph.execution import LangGraphExecution
from tests.langgraph_execution import make_langgraph_execution
from voidx.agent.adapters.langgraph.execution import AGENT_RESULT_PREVIEW_CHARS, _agent_result_preview
from voidx.agent.adapters.persistence.message_rows import RowMessageCacheEntry
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
from voidx.agent.domain.task.state import TaskState, ToolStatePatch, WorkflowRoute
from voidx.tooling.domain.context import ToolExecutionContext as ToolContext
from voidx.tooling.domain.result import ToolResult
from voidx.agent.adapters.tools.subagent import AgentResultContract, AgentTool
from voidx.tooling.application.registry import ToolRegistry
from voidx.presentation.output.dock import BottomInputDock, set_dock
from voidx.presentation.output.events import (
    AssistantStreamCommitted,
    AssistantStreamUpdated,
    DockEventConsumer,
    StatusUpdated,
    ToolResultAppended,
    TurnStarted,
    ui_events,
)


def _graph(tmp_path):
    from voidx.agent.adapters.langgraph.runtime.thread_context import (
        ThreadExecutionState,
        _CURRENT_THREAD_EXECUTION_STATE,
    )
    from voidx.agent.domain.agent_profile import (
        WorkflowRuntimeContext as PinnedWorkflowRuntimeContext,
    )
    from voidx.agent.domain.automation.workflow_dag import DEFAULT_WORKFLOW_DAG
    from voidx.agent.domain.turn_context import TurnExecutionContext

    bound = _CURRENT_THREAD_EXECUTION_STATE.get()
    if bound is None or bound.turn_context is None:
        _CURRENT_THREAD_EXECUTION_STATE.set(
            ThreadExecutionState(
                thread_id="test-thread",
                turn_context=TurnExecutionContext(
                    thread_id="test-thread",
                    session_id="test-session",
                    workspace=str(tmp_path),
                    workflow_context=PinnedWorkflowRuntimeContext(
                        dag=DEFAULT_WORKFLOW_DAG,
                        dag_revision=1,
                        dag_hash="test-default-workflow",
                        source="bundled",
                    ),
                ),
                workspace=str(tmp_path),
            )
        )
    cfg = Config(workspace=str(tmp_path))
    return make_langgraph_execution(cfg, api_key="test")


def _task_state_json(**kwargs):
    return TaskState(**kwargs).model_dump(mode="json")


def _edit_args(file_path: str) -> dict:
    return {
        "file_path": file_path,
        "edits": [{"operation": "replace", "lineno": 1, "prefix": "old", "suffix": "old", "new_string": "new"}],
    }


def _result_task_state(result: dict) -> TaskState:
    return TaskState.model_validate(result["task_state"])


class RecordingGoalStore:
    def __init__(self) -> None:
        self.records = []

    async def submit_goal_protocol(self, record, **kwargs):
        self.records.append(record)
        return record


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


def _child_result_contract(contract_type: str = "implementation_result") -> AgentResultContract:
    result_format = (
        "verdict=PASS|FAIL|NEEDS_CHANGE, findings, risks, verification_notes, next_actions"
        if contract_type == "review_result"
        else "status, files_changed, tests_run, risks, followups"
    )
    return AgentResultContract(format=result_format)


def _subagent_contract_kwargs(
    *,
    goal_type: str = "inspect",
    desc: str = "Inspect the workspace",
    join: str = "review",
    leave: str = "review",
    contract_type: str = "inspection_result",
) -> dict:
    return {
        "goal_resolution": _child_goal_resolution(goal_type, desc=desc, join=join, leave=leave),
        "result_contract": _child_result_contract(contract_type),
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

    graph.tools.replace("todo", FakeTodoTool(), "fake todo", {"type": "object", "properties": {}})
    graph.tools.replace("read", FakeReadTool(), "fake read", {"type": "object", "properties": {}})

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
    todo_observation = result["messages"][0].additional_kwargs["voidx_tool_observation"]
    read_observation = result["messages"][1].additional_kwargs["voidx_tool_observation"]
    assert todo_observation == {
        "source": "tool_executor",
        "executed": True,
        "synthetic": False,
        "status": "success",
        "fallback_eligible": False,
    }
    assert read_observation == {
        "source": "tool_executor",
        "executed": True,
        "synthetic": False,
        "status": "success",
        "fallback_eligible": True,
    }
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

    graph.tools.replace("todo", FakeTodoTool(), "fake todo", {"type": "object", "properties": {}})
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

    graph.tools.replace("todo", FakeTodoTool(), "fake todo", {"type": "object", "properties": {}})
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

    graph.tools.replace("todo", FakeTodoTool(), "fake todo", {"type": "object", "properties": {}})
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
    from voidx.presentation.output.events import WarningAppended
    assert not any(isinstance(e, WarningAppended) for e in emitted)

@pytest.mark.asyncio
async def test_subagent_full_output_reaches_orchestrator(tmp_path, monkeypatch):

    graph = _graph(tmp_path)
    child_output = "\n".join(f"child final line {index}" for index in range(1, 8))

    class FakeSubagentModel:
        def bind_tools(self, _tool_defs):
            return self

        async def astream(self, _messages):
            yield AIMessageChunk(content=[{"type": "thinking", "text": "child hidden thought"}])
            yield AIMessageChunk(content=child_output)

    monkeypatch.setattr(
        graph,
        "_model_factory",
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
                        "mode": "review",
                        "goal": "审查 auth flow",
                        "detail": "Inspect the auth flow and report concrete findings.",
                        "scope": "src/voidx/auth.py",
                    },
                    "id": "call_agent",
                    "type": "tool_call",
                }],
        )

        spawn_result = await graph._execute_tools({
            "messages": [parent],
            "workspace": str(tmp_path),
            "persona": "voidx",
            "plan_mode": False,
        })
        await ui_events.drain()

        spawn_tool_messages = [message for message in spawn_result["messages"] if isinstance(message, ToolMessage)]
        assert spawn_tool_messages[0].tool_call_id == "call_agent"
        assert "[running]" in spawn_tool_messages[0].content
        assert "run_id:" in spawn_tool_messages[0].content
        child_run = next(run for run in graph.agent_gateway.list_runs() if run.agent_type == "sub")

        wait_parent = AIMessage(
            content="",
            tool_calls=[{
                "name": "agent_control",
                "args": {
                    "action": "wait",
                    "run_id": child_run.run_id,
                },
                "id": "call_wait_agent",
                "type": "tool_call",
            }],
        )
        wait_result = await graph._execute_tools({
            "messages": [wait_parent],
            "workspace": str(tmp_path),
            "persona": "voidx",
            "plan_mode": False,
        })
        await ui_events.drain()

        assistant = next(node for node in test_dock.tree.root.children if node.node_type == "assistant")
        subagent = next((node for node in assistant.children if node.node_type == "subagent"), None)
        child_streams = [] if subagent is None else [
            node for node in subagent.children
            if node.node_type == "assistant" and "child final line" in node.header
        ]

        rendered = "\n".join(test_dock.tree.render(120))
        assert child_streams == []
        assert "child hidden thought" not in rendered
        assert "child final line 7" not in rendered
        wait_tool_messages = [message for message in wait_result["messages"] if isinstance(message, ToolMessage)]
        assert wait_tool_messages[0].tool_call_id == "call_wait_agent"
        wait_content = str(wait_tool_messages[0].content)
        assert "[completed]" in wait_content
        assert "Result:" in wait_content
        assert child_output in wait_content
        assert not any(isinstance(message, AIMessage) and message.content == child_output for message in wait_result["messages"])
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

    graph._workspace_write_lock = FakeRunManager()

    class FakeWriteTool:
        id = "write"
        description = "fake write"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            events.append(("execute", ctx.session_id))
            return ToolResult(output="write output")

    graph.tools.replace("write", FakeWriteTool(), "fake write", {"type": "object", "properties": {}})

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

    graph._workspace_write_lock = FakeRunManager()

    class FailingWriteTool:
        id = "write"
        description = "failing write"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            events.append(("execute", ctx.session_id))
            raise RuntimeError("boom")

    graph.tools.replace("write", FailingWriteTool(), "failing write", {"type": "object", "properties": {}})

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

    graph._workspace_write_lock = FakeRunManager()

    class FakeReadTool:
        id = "read"
        description = "fake read"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            events.append(("execute", ctx.session_id))
            return ToolResult(output="read output")

    graph.tools.replace("read", FakeReadTool(), "fake read", {"type": "object", "properties": {}})

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
    from voidx.agent.adapters.langgraph.runtime.tool_executor import executor as executor_module
    from voidx.presentation.output.events.bus import UiEventTimeout

    graph = _graph(tmp_path)

    class FakeReadTool:
        id = "read"
        description = "fake read"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            return ToolResult(output="should not execute")

    graph.tools.replace("read", FakeReadTool(), "fake read", {"type": "object", "properties": {}})

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
    from voidx.agent.adapters.langgraph.runtime.topology import route_after_execute_tools
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

    graph.tools.replace("read", FakeReadTool(), "fake read", {"type": "object", "properties": {}})

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
    from voidx.agent.adapters.langgraph.runtime.tool_executor import executor as executor_module

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

    graph.tools.replace("read", SlowReadTool(), "slow read", {"type": "object", "properties": {}})

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
async def test_execute_tools_loop_policy_allows_bound_tool_and_denies_unbound_tool(tmp_path):
    from voidx.agent.domain.automation.loop import LoopToolView
    from voidx.agent.adapters.langgraph.runtime.thread_context import current_thread_execution_state

    graph = _graph(tmp_path)
    executed: list[str] = []

    class FakeReadTool:
        id = "read"
        description = "fake read"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            executed.append("read")
            return ToolResult(output="read output")

    class FakeWriteTool:
        id = "write"
        description = "fake write"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            executed.append("write")
            return ToolResult(output="write output")

    state_context = current_thread_execution_state()
    assert state_context is not None
    state_context.tool_policy = LoopToolView.default(workflow_enabled=False).bind({"read", "write"})

    graph.tools.replace("read", FakeReadTool(), "fake read", {"type": "object", "properties": {}})
    graph.tools.replace("write", FakeWriteTool(), "fake write", {"type": "object", "properties": {}})
    parent = AIMessage(
        content="",
        tool_calls=[
            {"name": "read", "args": {}, "id": "call_read", "type": "tool_call"},
            {"name": "write", "args": {}, "id": "call_write", "type": "tool_call"},
        ],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
    })

    assert executed == ["read"]
    assert [message.tool_call_id for message in result["messages"]] == ["call_read", "call_write"]
    assert result["messages"][0].content == "read output"
    assert result["messages"][1].status == "error"
    assert result["messages"][1].content == "Tool denied: tool_not_bound"





def test_profile_policy_denial_records_pinned_audit_metadata() -> None:
    from voidx.agent.adapters.langgraph.runtime.tool_executor.executor import (
        _profile_policy_denial,
    )
    from voidx.agent.domain.tool_policy import ToolPolicyDecision
    from voidx.tooling.domain.capability import ToolCapability

    executed = _profile_policy_denial(
        {"name": "Edit", "args": {}, "id": "call_edit"},
        ToolPolicyDecision(
            allowed=False,
            reason="profile_blocked",
            canonical_tool="replace",
            snapshot_hash="snapshot-1",
            phase="turn",
            capability="execution_gated",
        ),
    )

    assert executed.result.metadata == {
        "error": True,
        "tool_denied": True,
        "snapshot_hash": "snapshot-1",
        "phase": "turn",
        "decision": "deny",
        "reason": "profile_blocked",
        "canonical_tool": "replace",
        "capability": "execution_gated",
    }


@pytest.mark.asyncio
async def test_execute_tools_rechecks_profile_policy_after_authorization(tmp_path):
    from voidx.agent.adapters.langgraph.runtime.thread_context import (
        current_thread_execution_state,
    )
    from voidx.agent.domain.agent_profile import ResourcePolicy
    from voidx.agent.domain.run_config import resolve_run_config
    from voidx.agent.domain.tool_policy import CodingToolPolicy, ProfileToolPolicy

    graph = _graph(tmp_path)
    executed: list[str] = []

    class FakeClarifyTool:
        id = "clarify"
        description = "fake clarify"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            executed.append("clarify")
            return ToolResult(output="clarified")

    graph.tools.replace("clarify", FakeClarifyTool(), "fake clarify", {})
    state_context = current_thread_execution_state()
    assert state_context is not None
    state_context.tool_policy = ProfileToolPolicy(
        baseline=CodingToolPolicy(),
        resource_policy=ResourcePolicy(hitl_mode="autonomous"),
        run_config=resolve_run_config("single"),
        snapshot_hash="snapshot-1",
        phase="turn",
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
                "name": "clarify",
                "args": {"question": "continue?"},
                "id": "call_clarify",
                "type": "tool_call",
            }
        ],
    )

    result = await graph._execute_tools(
        {
            "messages": [parent],
            "workspace": str(tmp_path),
            "persona": "voidx",
            "plan_mode": False,
        }
    )

    assert executed == []
    assert len(result["messages"]) == 1
    assert result["messages"][0].status == "error"
    assert result["messages"][0].content == (
        "Tool denied: hitl_interaction_unavailable"
    )
@pytest.mark.asyncio
async def test_execute_tools_continues_after_legacy_tool_timeout(tmp_path):
    from voidx.agent.adapters.langgraph.runtime.topology import route_after_execute_tools

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

    graph.tools.replace(
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
    from voidx.agent.adapters.langgraph.runtime.topology import route_after_execute_tools

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

    graph.tools.replace(
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
    from voidx.agent.adapters.langgraph.runtime.tool_executor import executor as executor_module
    from voidx.presentation.output.events.bus import UiEventTimeout

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

    graph.tools.replace("read", FakeReadTool(), "fake read", {"type": "object", "properties": {}})
    graph.tools.replace("bash", FakeBashTool(), "fake bash", {"type": "object", "properties": {}})

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
    from voidx.agent.adapters.langgraph.runtime.topology import route_after_execute_tools
    assert route_after_execute_tools(result) == "end"
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    assert not pending


@pytest.mark.asyncio
async def test_ui_timeout_skips_event_drain(tmp_path, monkeypatch):
    from voidx.agent.adapters.langgraph.runtime.tool_executor import executor as executor_module
    from voidx.presentation.output.events.bus import UiEventTimeout

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
    from voidx.agent.adapters.langgraph.runtime.topology import route_after_execute_tools
    from voidx.tooling.domain.result import tool_timeout_metadata

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

    graph.tools.replace(
        "checkpoint",
        TimedOutCheckpointTool(),
        "timed out checkpoint",
        {"type": "object", "properties": {}},
    )
    graph.tools.replace(
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
    from voidx.agent.adapters.langgraph.runtime.runtime_guards import tool_call_key
    from voidx.agent.adapters.langgraph.runtime.tool_executor import executor as executor_module
    from voidx.presentation.output.events.bus import UiEventTimeout

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

    graph.tools.replace(
        "read",
        MixedReadTool(),
        "mixed read",
        {"type": "object", "properties": {}},
    )
    graph.tools.replace(
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
    from voidx.agent.adapters.langgraph.runtime.tool_executor.guards import _record_runtime_guard_outcomes
    from voidx.agent.adapters.langgraph.runtime.tool_executor.types import _ExecutedTool, _tool_result_ok

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
    from voidx.agent.adapters.langgraph.runtime.tool_executor.guards import _record_runtime_guard_outcomes
    from voidx.agent.adapters.langgraph.runtime.tool_executor.types import _ExecutedTool, _tool_result_ok
    from voidx.tooling.domain.result import tool_timeout_metadata

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
    from voidx.agent.adapters.langgraph.runtime.tool_executor.helpers import _execute_approved_batch
    from voidx.agent.adapters.langgraph.runtime.tool_executor.types import _ExecutedTool

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
async def test_execute_approved_batch_keeps_runtime_grants_for_file_lock_waiters():
    from voidx.agent.adapters.langgraph.runtime.tool_executor.helpers import _execute_approved_batch
    from voidx.agent.adapters.langgraph.runtime.tool_executor.types import _ExecutedTool
    from voidx.tooling.adapters.permission.in_memory_state import create_permission_service
    from voidx.tooling.domain.grants import AccessGrant

    service = create_permission_service()
    grant = AccessGrant(
        path="/external/same.txt",
        access="write",
        object_type="file",
        persistence="runtime",
    )
    await service.add_grant(grant)
    grant_seen: list[bool] = []

    async def execute_one(tool_call: dict) -> _ExecutedTool:
        async with service.execution_lease_for_tool("write"):
            grant_seen.append(grant in service.grant_snapshot())
            await asyncio.sleep(0)
            return _ExecutedTool(
                message=None,
                result=ToolResult(output="ok"),
                tool_call=tool_call,
            )

    approved = [
        {
            "name": "write",
            "args": {"file_path": "/external/same.txt", "op": "write", "new_string": "a"},
            "id": "call_a",
            "type": "tool_call",
        },
        {
            "name": "write",
            "args": {"file_path": "/external/same.txt", "op": "write", "new_string": "b"},
            "id": "call_b",
            "type": "tool_call",
        },
    ]
    host = SimpleNamespace(
        config=Config(workspace="."),
        _permission=service,
        _ui=SimpleNamespace(via_events=lambda: False),
    )

    await _execute_approved_batch(
        approved,
        host=host,
        guard_state=RuntimeGuardState(),
        execute_one_fn=execute_one,
    )

    assert grant_seen == [True, True]
    assert grant not in service.grant_snapshot()


@pytest.mark.asyncio
async def test_real_event_bus_stall_terminates_turn_without_drain(tmp_path, monkeypatch):
    """Regression: a real blocked UiEventBus consumer must cause a bounded turn
    termination that skips drain and leaves no pending task."""
    from voidx.agent.adapters.langgraph.runtime.tool_executor import executor as executor_module
    from voidx.agent.adapters.langgraph.runtime.topology import route_after_execute_tools
    from voidx.presentation.output.events.bus import UiEventBus
    from voidx.presentation.output.events.schema import ToolStarted

    graph = _graph(tmp_path)

    class FakeReadTool:
        id = "read"
        description = "fake read"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            return ToolResult(output="should not execute")

    graph.tools.replace("read", FakeReadTool(), "fake read", {"type": "object", "properties": {}})

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
    from voidx.agent.adapters.langgraph.runtime.tool_executor import executor as executor_module
    from voidx.agent.adapters.langgraph.runtime.topology import route_after_execute_tools
    from voidx.presentation.output.events.bus import UiEventTimeout

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

    graph.tools.replace("read", FakeReadTool(), "fake read", {"type": "object", "properties": {}})
    graph.tools.replace("checkpoint", FakeCheckpointTool(), "fake checkpoint", {"type": "object", "properties": {}})
    graph.tools.replace("bash", FakeBashTool(), "fake bash", {"type": "object", "properties": {}})

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
    from voidx.agent.adapters.langgraph.runtime.tool_executor import executor as executor_module
    from voidx.agent.adapters.langgraph.runtime.topology import route_after_execute_tools
    from voidx.presentation.output.events.bus import UiEventTimeout

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

    graph.tools.replace("checkpoint", FakeCheckpointTool(), "fake checkpoint", {"type": "object", "properties": {}})
    graph.tools.replace("bash", FakeBashTool(), "fake bash", {"type": "object", "properties": {}})

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
    from voidx.agent.adapters.langgraph.runtime.tool_executor import executor as executor_module
    from voidx.presentation.output.events.bus import UiEventTimeout

    graph = _graph(tmp_path)

    class FakeReadTool:
        id = "read"
        description = "fake read"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            return ToolResult(output="read output")

    graph.tools.replace("read", FakeReadTool(), "fake read", {"type": "object", "properties": {}})

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


@pytest.mark.asyncio
async def test_execute_tools_skips_calls_after_loop_commit_in_same_batch(tmp_path):
    from voidx.agent.domain.automation.loop import LOOP_PROFILE, LoopSpec
    from voidx.agent.domain.turn_context import TurnExecutionContext
    from voidx.agent.adapters.langgraph.runtime.thread_context import (
        ThreadExecutionState,
        _CURRENT_THREAD_EXECUTION_STATE,
    )
    from voidx.agent.application.automation.loop.controller import LoopAttemptController
    from voidx.agent.adapters.tools.automation.loop import LoopTool

    events: list[object] = []

    class RecordingConsumer:
        def handle(self, event):
            events.append(event)
            return None

    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    if ui_events.is_running:
        await ui_events.stop()
    ui_events.start(RecordingConsumer())
    controller = LoopAttemptController(spec=LoopSpec(prompt="check"))
    token = _CURRENT_THREAD_EXECUTION_STATE.set(ThreadExecutionState(
        thread_id="loop-thread",
        turn_context=TurnExecutionContext(
            thread_id="loop-thread",
            session_id="loop-session",
            runtime_profile=LOOP_PROFILE,
            workspace=str(tmp_path),
            loop_controller=controller,
        ),
        workspace=str(tmp_path),
    ))
    try:
        graph = _graph(tmp_path)
        graph.tools.replace("loop", LoopTool(), "loop", {"type": "object", "properties": {}})

        executed = []

        class FakeMcpTool:
            id = "mcp"
            description = "fake mcp"

            def parameters_schema(self):
                return {"type": "object", "properties": {}}

            async def execute(self, args: dict, ctx) -> ToolResult:
                executed.append("mcp")
                return ToolResult(output="mcp output")

        graph.tools.replace("mcp", FakeMcpTool(), "fake mcp", {"type": "object", "properties": {}})

        async def allow_all(tool_calls, plan_mode: bool, session_id: str, interaction_mode=None):
            return tool_calls, []

        graph._authorize_tool_calls = allow_all
        parent = AIMessage(
            content="",
            tool_calls=[
                {"name": "loop", "args": {"operation": "commit", "outcome": "continue", "summary": "done", "next_delay_seconds": 1800}, "id": "call_commit", "type": "tool_call"},
                {"name": "mcp", "args": {"op": "call"}, "id": "call_mcp", "type": "tool_call"},
            ],
        )

        result = await graph._execute_tools({
            "messages": [parent],
            "workspace": str(tmp_path),
            "persona": "voidx",
            "plan_mode": False,
        })
        await ui_events.drain()

        assert controller.final_decision() is not None
        assert executed == [], "mcp must not run after the loop commit"
        contents = {m.tool_call_id: m.content for m in result["messages"] if isinstance(m, ToolMessage)}
        assert "call_commit" in contents and "call_mcp" in contents
        assert "skipped" in contents["call_mcp"]
        assert result["should_continue"] is False

        assistant_messages = [message for message in result["messages"] if isinstance(message, AIMessage)]
        assert assistant_messages[-1].content == "done"
        assert any(isinstance(event, AssistantStreamUpdated) and event.text == "done" for event in events)
        assert any(isinstance(event, AssistantStreamCommitted) for event in events)
    finally:
        _CURRENT_THREAD_EXECUTION_STATE.reset(token)
        await ui_events.stop()
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)


@pytest.mark.asyncio
async def test_execute_tools_stops_turn_after_goal_intake_init(tmp_path):
    from voidx.agent.domain.automation.goal import GOAL_PROFILE
    from voidx.agent.domain.turn_context import TurnExecutionContext
    from voidx.agent.application.automation.goal.intake_controller import GoalIntakeController
    from voidx.agent.adapters.langgraph.runtime.thread_context import (
        ThreadExecutionState,
        _CURRENT_THREAD_EXECUTION_STATE,
    )
    from voidx.agent.adapters.tools.automation.goal import GoalInitTool

    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    if ui_events.is_running:
        await ui_events.stop()
    ui_events.start(DockEventConsumer(test_dock))
    controller = GoalIntakeController()
    token = _CURRENT_THREAD_EXECUTION_STATE.set(ThreadExecutionState(
        thread_id="goal-intake-thread",
        turn_context=TurnExecutionContext(
            thread_id="goal-intake-thread",
            session_id="goal-session",
            runtime_profile=GOAL_PROFILE,
            workspace=str(tmp_path),
            goal_intake_controller=controller,
            goal_phase="intake",
            goal_store=RecordingGoalStore(),
            goal_generation="generation-1",
            goal_parent_session_id="goal-parent-session",
            goal_main_session_id="goal-session",
            goal_turn_id="turn-init",
            goal_attempt_number=0,
        ),
        workspace=str(tmp_path),
    ))
    try:
        graph = _graph(tmp_path)
        graph.tools.replace("goal_init", GoalInitTool(), "goal_init", {"type": "object", "properties": {}})

        executed = []

        class FakeMcpTool:
            id = "mcp"
            description = "fake mcp"

            def parameters_schema(self):
                return {"type": "object", "properties": {}}

            async def execute(self, args: dict, ctx) -> ToolResult:
                executed.append("mcp")
                return ToolResult(output="mcp output")

        graph.tools.replace("mcp", FakeMcpTool(), "fake mcp", {"type": "object", "properties": {}})

        async def allow_all(tool_calls, plan_mode: bool, session_id: str, interaction_mode=None):
            return tool_calls, []

        graph._authorize_tool_calls = allow_all
        parent = AIMessage(
            content="",
            tool_calls=[
                {"name": "goal_init", "args": {"objective": "ship", "acceptance_condition": "green", "achievement_method": "", "max_attempts": 20}, "id": "call_init", "type": "tool_call"},
                {"name": "mcp", "args": {"op": "call"}, "id": "call_mcp", "type": "tool_call"},
            ],
        )

        result = await graph._execute_tools({
            "messages": [parent],
            "workspace": str(tmp_path),
            "persona": "voidx",
            "plan_mode": False,
        })
        await ui_events.drain()

        assert controller.final_spec() is not None
        assert executed == [], "no tool may run after the intake spec was submitted"
        contents = {m.tool_call_id: m.content for m in result["messages"] if isinstance(m, ToolMessage)}
        assert "call_init" in contents and "call_mcp" in contents
        assert "skipped" in contents["call_mcp"]
        assert result["should_continue"] is False
    finally:
        _CURRENT_THREAD_EXECUTION_STATE.reset(token)
        await ui_events.stop()


@pytest.mark.asyncio
async def test_execute_tools_stops_turn_after_goal_intake_cancel(tmp_path):
    from voidx.agent.domain.automation.goal import GOAL_PROFILE
    from voidx.agent.domain.turn_context import TurnExecutionContext
    from voidx.agent.application.automation.goal.intake_controller import GoalIntakeController
    from voidx.agent.adapters.langgraph.runtime.thread_context import (
        ThreadExecutionState,
        _CURRENT_THREAD_EXECUTION_STATE,
    )
    from voidx.agent.adapters.tools.automation.goal import GoalInitTool

    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    if ui_events.is_running:
        await ui_events.stop()
    ui_events.start(DockEventConsumer(test_dock))
    controller = GoalIntakeController()
    token = _CURRENT_THREAD_EXECUTION_STATE.set(ThreadExecutionState(
        thread_id="goal-intake-thread",
        turn_context=TurnExecutionContext(
            thread_id="goal-intake-thread",
            session_id="goal-session",
            runtime_profile=GOAL_PROFILE,
            workspace=str(tmp_path),
            goal_intake_controller=controller,
            goal_phase="intake",
            goal_store=RecordingGoalStore(),
            goal_generation="generation-1",
            goal_parent_session_id="goal-parent-session",
            goal_main_session_id="goal-session",
            goal_turn_id="turn-init-cancel",
            goal_attempt_number=0,
        ),
        workspace=str(tmp_path),
    ))
    try:
        graph = _graph(tmp_path)
        graph.tools.replace("goal_init", GoalInitTool(), "goal_init", {"type": "object", "properties": {}})

        class CancelApp:
            async def ask_choice(self, prompt, choices, **kwargs):
                return "cancelled"

            async def ask_text(self, prompt, **kwargs):
                return None

        graph._ui.bind_frontend( CancelApp())

        executed = []

        class FakeMcpTool:
            id = "mcp"
            description = "fake mcp"

            def parameters_schema(self):
                return {"type": "object", "properties": {}}

            async def execute(self, args: dict, ctx) -> ToolResult:
                executed.append("mcp")
                return ToolResult(output="mcp output")

        graph.tools.replace("mcp", FakeMcpTool(), "fake mcp", {"type": "object", "properties": {}})

        async def allow_all(tool_calls, plan_mode: bool, session_id: str, interaction_mode=None):
            return tool_calls, []

        graph._authorize_tool_calls = allow_all
        parent = AIMessage(
            content="",
            tool_calls=[
                {"name": "goal_init", "args": {"objective": "ship", "acceptance_condition": "green", "achievement_method": "", "max_attempts": 20}, "id": "call_init", "type": "tool_call"},
                {"name": "mcp", "args": {"op": "call"}, "id": "call_mcp", "type": "tool_call"},
            ],
        )

        result = await graph._execute_tools({
            "messages": [parent],
            "workspace": str(tmp_path),
            "persona": "voidx",
            "plan_mode": False,
        })
        await ui_events.drain()

        assert controller.final_spec() is None
        assert controller.cancelled is True
        assert executed == [], "no tool may run after the intake was cancelled"
        assert result["should_continue"] is False
    finally:
        _CURRENT_THREAD_EXECUTION_STATE.reset(token)
        await ui_events.stop()


@pytest.mark.asyncio
async def test_execute_tools_stops_turn_after_goal_evaluator_decision(tmp_path):
    from voidx.agent.domain.automation.goal import GOAL_PROFILE
    from voidx.agent.domain.turn_context import TurnExecutionContext
    from voidx.agent.application.automation.goal.controller import GoalController
    from voidx.agent.adapters.langgraph.runtime.thread_context import (
        ThreadExecutionState,
        _CURRENT_THREAD_EXECUTION_STATE,
    )
    from voidx.agent.adapters.tools.automation.goal import GoalDecisionTool

    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    if ui_events.is_running:
        await ui_events.stop()
    ui_events.start(DockEventConsumer(test_dock))
    controller = GoalController(attempt_id="goal-thread:1")
    token = _CURRENT_THREAD_EXECUTION_STATE.set(ThreadExecutionState(
        thread_id="goal-thread",
        turn_context=TurnExecutionContext(
            thread_id="goal-thread",
            session_id="goal-session",
            runtime_profile=GOAL_PROFILE,
            workspace=str(tmp_path),
            goal_controller=controller,
            goal_phase="evaluator",
            goal_store=RecordingGoalStore(),
            goal_generation="generation-1",
            goal_parent_session_id="goal-parent-session",
            goal_evaluator_session_id="goal-session",
            goal_turn_id="turn-decision",
            goal_attempt_number=1,
        ),
        workspace=str(tmp_path),
    ))
    try:
        graph = _graph(tmp_path)
        graph.tools.replace("goal_decision", GoalDecisionTool(), "goal_decision", {"type": "object", "properties": {}})

        executed = []

        class FakeMcpTool:
            id = "mcp"
            description = "fake mcp"

            def parameters_schema(self):
                return {"type": "object", "properties": {}}

            async def execute(self, args: dict, ctx) -> ToolResult:
                executed.append("mcp")
                return ToolResult(output="mcp output")

        graph.tools.replace("mcp", FakeMcpTool(), "fake mcp", {"type": "object", "properties": {}})

        async def allow_all(tool_calls, plan_mode: bool, session_id: str, interaction_mode=None):
            return tool_calls, []

        graph._authorize_tool_calls = allow_all
        parent = AIMessage(
            content="",
            tool_calls=[
                {"name": "goal_decision", "args": {"status": "finished", "summary": "done", "evidence": ["verified"], "reason": "", "next_hint": "", "missing_evidence": [], "progress": "meaningful"}, "id": "call_decision", "type": "tool_call"},
                {"name": "mcp", "args": {"op": "call"}, "id": "call_mcp", "type": "tool_call"},
            ],
        )

        result = await graph._execute_tools({
            "messages": [parent],
            "workspace": str(tmp_path),
            "persona": "voidx",
            "plan_mode": False,
        })
        await ui_events.drain()

        assert controller.final_decision() is not None
        assert executed == [], "no tool may run after the evaluator decision was submitted"
        contents = {m.tool_call_id: m.content for m in result["messages"] if isinstance(m, ToolMessage)}
        assert "skipped" in contents["call_mcp"]
        assert result["should_continue"] is False
    finally:
        _CURRENT_THREAD_EXECUTION_STATE.reset(token)
        await ui_events.stop()
