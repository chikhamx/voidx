"""Regression tests for core graph behavior."""

from tests.tool_registry import build_registry
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
from voidx.agent.domain.task.state import TaskState, ToolStatePatch
from voidx.agent.domain.automation.workflow import WorkflowRoute
from voidx.tooling.domain.context import ToolExecutionContext as ToolContext
from voidx.tooling.domain.result import ToolResult
from voidx.agent.adapters.tools.subagent import AgentResultContract, AgentTool
from voidx.tooling.application.registry import ToolRegistry
from voidx.presentation.output.dock import BottomInputDock, set_dock
from voidx.presentation.output.events import DockEventConsumer, TurnStarted, ui_events


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
async def test_run_subagent_wall_clock_guard_terminates_at_boundary(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module
    from voidx.config import SubagentBudgetConfig
    from voidx.agent.adapters.langgraph.runtime.subagent_convergence import FINAL_CONVERGENCE_GUIDANCE

    executed_tools: list[str] = []
    calls: list[tuple[object, list]] = []

    class BoundModel:
        pass

    class FakeModel:
        def __init__(self):
            self.bound = BoundModel()

        def bind_tools(self, _tool_defs):
            return self.bound

    model = FakeModel()

    class FakeToolRegistry:
        def filtered_copy(self, _allowed_ids):
            return self

        def ids(self):
            return ["read"]

        def serialize_definitions(self):
            return [{"name": "read", "description": "read", "input_schema": {}}]

        async def execute_tool(self, tid, _targs, _ctx):
            executed_tools.append(tid)
            return ToolResult(output=f"{tid} ok")

    expired = False

    async def fake_stream_llm(current_model, messages, _renderer, _protocol, **kwargs):
        nonlocal expired
        calls.append((current_model, list(messages)))
        if len(calls) == 1:
            expired = True
            return AIMessage(
                content="finding before timeout",
                tool_calls=[{
                    "name": "read",
                    "args": {},
                    "id": "call_read",
                    "type": "tool_call",
                }],
            )
        return AIMessage(content="status: partial\nfindings: finding before timeout")

    def fake_monotonic():
        return 2.0 if expired else 0.0

    monkeypatch.setattr(subagent_module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)
    monkeypatch.setattr(subagent_module, "ToolRegistry", FakeToolRegistry)

    run_metadata: dict[str, object] = {}
    output = await subagent_module.run_subagent(
        AgentDef(
            name="explore",
            description="test",
            when_to_use="test",
            can_write=False,
            can_delegate=False,
        ),
        "Inspect child path",
        "test-key",
        Config(
            workspace=str(tmp_path),
            subagent_budget=SubagentBudgetConfig(
                wall_clock_seconds=2.0,
                soft_warn_ratio=0.8,
            ),
        ),
        runtime_persona="explore",
        run_metadata=run_metadata,
        **_subagent_contract_kwargs(desc="Inspect child path"),
        debug=False,
    )

    assert executed_tools == []
    assert len(calls) == 2
    assert isinstance(calls[0][0], BoundModel)
    assert calls[1][0] is model
    assert FINAL_CONVERGENCE_GUIDANCE in calls[1][1][-1].content
    assert output == "status: partial\nfindings: finding before timeout"
    assert run_metadata["finish_reason"] == "time_limit"



@pytest.mark.asyncio
async def test_wall_clock_soft_crossing_after_llm_is_guided_on_next_request(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module
    from voidx.config import SubagentBudgetConfig
    from voidx.agent.adapters.langgraph.runtime.subagent_convergence import SOFT_CONVERGENCE_GUIDANCE

    calls: list[list] = []
    expired_soft = False

    class FakeModel:
        def bind_tools(self, _tool_defs):
            return self

    class FakeToolRegistry:
        def filtered_copy(self, _allowed_ids):
            return self

        def ids(self):
            return ["read"]

        def serialize_definitions(self):
            return [{"name": "read", "description": "read", "input_schema": {}}]

        async def execute_tool(self, _tid, _args, _ctx):
            return ToolResult(output="finding")

    async def fake_stream_llm(_model, messages, _renderer, _protocol, **kwargs):
        nonlocal expired_soft
        calls.append(list(messages))
        if len(calls) == 1:
            expired_soft = True
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": "read",
                    "args": {},
                    "id": "read-1",
                    "type": "tool_call",
                }],
            )
        return AIMessage(content=(
            "status: completed\n"
            "files_changed: none\n"
            "tests_run: none\n"
            "risks: none\n"
            "followups: none"
        ))

    def fake_monotonic():
        return 8.0 if expired_soft else 0.0

    monkeypatch.setattr(subagent_module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)
    monkeypatch.setattr(subagent_module, "ToolRegistry", FakeToolRegistry)

    output = await subagent_module.run_subagent(
        AgentDef(
            name="explore",
            description="test",
            when_to_use="test",
            can_write=False,
            can_delegate=False,
        ),
        "Inspect child path",
        "test-key",
        Config(
            workspace=str(tmp_path),
            subagent_budget=SubagentBudgetConfig(
                wall_clock_seconds=10.0,
                soft_warn_ratio=0.8,
            ),
        ),
        runtime_persona="explore",
        **_subagent_contract_kwargs(desc="Inspect child path"),
        debug=False,
    )

    assert output.startswith("status: completed")
    assert len(calls) == 2
    assert any(
        isinstance(message, HumanMessage)
        and SOFT_CONVERGENCE_GUIDANCE in str(message.content)
        for message in calls[1]
    )

@pytest.mark.asyncio
async def test_run_subagent_repetitive_guard_runs_before_authorization(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module

    stream_calls: list[list] = []
    authorized_batches: list[list[str]] = []
    executed_tools: list[str] = []

    class FakeModel:
        def bind_tools(self, _tool_defs):
            return self

    class FakeToolRegistry:
        def filtered_copy(self, _allowed_ids):
            return self

        def ids(self):
            return ["read"]

        def serialize_definitions(self):
            return [{"name": "todo", "description": "todo", "input_schema": {}}]

        async def execute_tool(self, tid, _targs, _ctx):
            executed_tools.append(tid)
            return ToolResult(output="todo output")

    async def fake_stream_llm(_model, messages, _renderer, _protocol, **kwargs):
        stream_calls.append(list(messages))
        return AIMessage(
            content="",
            tool_calls=[{
                "name": "todo",
                "args": {"todos": []},
                "id": f"call_todo_{len(stream_calls)}",
                "type": "tool_call",
            }],
        )

    async def authorize(tool_calls):
        authorized_batches.append([call.get("name", "") for call in tool_calls])
        return list(tool_calls), []

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)
    monkeypatch.setattr(subagent_module, "ToolRegistry", FakeToolRegistry)

    output = await subagent_module.run_subagent(
        AgentDef(
            name="explore",
            description="test",
            when_to_use="test",
            can_write=False,
            can_delegate=False,
        ),
        "Inspect child path",
        "test-key",
        Config(workspace=str(tmp_path)),
        runtime_persona="explore",
        **_subagent_contract_kwargs(desc="Inspect child path"),
        authorize_tools=authorize,
        debug=False,
    )

    assert "Runtime guard" not in output
    assert "task could not be fully completed" in output
    final_contents = [str(message.content) for message in stream_calls[-1]]
    assert not any("Runtime guard" in content for content in final_contents)
    assert not any("repeated" in content.lower() for content in final_contents)
    assert executed_tools == ["todo", "todo"]
    assert authorized_batches == [["todo"], ["todo"]]


@pytest.mark.asyncio
async def test_subagent_todo_updates_sink_with_current_tool_message(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module

    stream_calls: list[list] = []
    todo_states: list[object] = []

    class FakeModel:
        def bind_tools(self, _tool_defs):
            return self

    async def fake_stream_llm(_model, messages, _renderer, _protocol, **kwargs):
        stream_calls.append(list(messages))
        if len(stream_calls) == 1:
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": "todo",
                    "args": {"todos": [{"id": "inspect", "content": "inspect child path", "status": "active"}]},
                    "id": "call_todo",
                    "type": "tool_call",
                }],
            )
        return AIMessage(content="done")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    output = await subagent_module.run_subagent(
        AgentDef(
            name="explore",
            description="test",
            when_to_use="test",
            can_write=False,
            can_delegate=False,
        ),
        "Inspect the workspace",
        "test-key",
        Config(workspace=str(tmp_path)),
        **_subagent_contract_kwargs(),
        parent_tools=build_registry(),
        todo_state_sink=todo_states.append,
        debug=False,
    )

    assert output == "done"
    assert len(todo_states) == 1
    assert todo_states[0].active_items[0].content == "inspect child path"
    second_call_messages = stream_calls[1]
    assert any(
        isinstance(message, ToolMessage) and message.tool_call_id == "call_todo"
        for message in second_call_messages
    )


@pytest.mark.asyncio
async def test_subagent_empty_todo_does_not_clear_parent_state(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module

    from voidx.agent.application.runtime.task_tracker import TaskTracker

    todo_states: list[object] = []
    parent_tracker = TaskTracker()
    parent_tracker.set_todos_from_dict({
        "parent": {"content": "parent work", "status": "active"},
    })
    calls = 0

    class FakeModel:
        def bind_tools(self, _tool_defs):
            return self

    async def fake_stream_llm(_model, _messages, _renderer, _protocol, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": "todo",
                    "args": {"todos": []},
                    "id": "call_todo",
                    "type": "tool_call",
                }],
            )
        return AIMessage(content="done")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    output = await subagent_module.run_subagent(
        AgentDef(
            name="explore",
            description="test",
            when_to_use="test",
            can_write=False,
            can_delegate=False,
        ),
        "Inspect the workspace",
        "test-key",
        Config(workspace=str(tmp_path)),
        parent_tracker,
        **_subagent_contract_kwargs(),
        parent_tools=build_registry(tracker=parent_tracker),
        todo_state_sink=todo_states.append,
        debug=False,
    )

    assert output == "done"
    assert todo_states == []
    assert parent_tracker.get_todos() == {
        "parent": {"content": "parent work", "status": "active"},
    }




@pytest.mark.asyncio
async def test_subagent_todo_uses_local_tracker_and_queued_event(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module
    from voidx.agent.application.runtime.task_tracker import TaskTracker
    from voidx.agent.domain.ui_events import TodoUpdated
    from voidx.agent.ports.ui import NullAgentUiPort

    stream_calls: list[list] = []
    queued_events: list[object] = []
    direct_events: list[object] = []

    class FakeModel:
        def bind_tools(self, _tool_defs):
            return self

    class RecordingEvents:
        async def emit(self, event):
            queued_events.append(event)
            return True

        def emit_direct(self, event):
            direct_events.append(event)
            return True

    class RecordingUiPort(NullAgentUiPort):
        def __init__(self):
            super().__init__()
            self._events = RecordingEvents()

        def via_events(self) -> bool:
            return True

    async def fake_stream_llm(_model, messages, _renderer, _protocol, **kwargs):
        stream_calls.append(list(messages))
        if len(stream_calls) == 1:
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": "todo",
                    "args": {
                        "op": "write",
                        "todos": [{"id": "child", "content": "child work", "status": "active"}],
                    },
                    "id": "call_todo",
                    "type": "tool_call",
                }],
            )
        return AIMessage(content="done")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)
    parent_tracker = TaskTracker()
    parent_tracker.set_todos_from_dict({
        "parent": {"content": "parent work", "status": "active"},
    })

    output = await subagent_module.run_subagent(
        AgentDef(
            name="explore",
            description="test",
            when_to_use="test",
            can_write=False,
            can_delegate=False,
        ),
        "Inspect the workspace",
        "test-key",
        Config(workspace=str(tmp_path)),
        parent_tracker,
        **_subagent_contract_kwargs(),
        parent_tools=build_registry(tracker=parent_tracker),
        agent_id=7,
        ui_port=RecordingUiPort(),
        debug=False,
    )

    assert output == "done"
    assert parent_tracker.get_todos() == {
        "parent": {"content": "parent work", "status": "active"},
    }
    todo_events = [event for event in queued_events if isinstance(event, TodoUpdated)]
    assert len(todo_events) == 1
    assert todo_events[0].agent_id == 7
    assert todo_events[0].items[0].id == "child"
    assert direct_events == []
