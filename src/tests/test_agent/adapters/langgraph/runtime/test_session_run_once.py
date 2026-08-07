"""Regression tests for core graph behavior."""

from voidx.agent.domain.turn_context import TurnExecutionContext
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
from voidx.config import Config, Settings, UserProfile
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
from voidx.presentation.adapters.persistence.transcript_adapter import TranscriptSnapshotAdapter
from tests.presentation_ui import make_presentation_ui

runtime_ui_port = make_presentation_ui()
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
from voidx.presentation.output.events import (
    DockEventConsumer,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
    ui_events,
)


def _graph(tmp_path, *, session=None):
    cfg = Config(workspace=str(tmp_path))
    return make_langgraph_execution(
        cfg,
        api_key=None,
        session=session,
        presentation_snapshots=TranscriptSnapshotAdapter(runtime_ui_port),
    )


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
async def test_run_turn_persists_and_restores_transcript_snapshot(tmp_path):
    session = await create_session(workspace=str(tmp_path))
    try:
        graph = _graph(tmp_path, session=session)

        class FakeGraph:
            async def astream(self, initial, _config, *, stream_mode="values"):
                from voidx.presentation.output.dock import dock

                dock.append_thought("checked context", elapsed=1.0)
                tool = dock.start_tool(
                    "Reading",
                    'file_path="src/app.py"',
                    tool_call_id="call_read",
                    tool_name="read",
                )
                dock.append_tool_result(
                    "src/app.py\nprint('ok')",
                    parent=tool,
                    tool_call_id="call_read",
                    collapsed=False,
                )
                yield {"messages": list(initial["messages"]) + [AIMessage(content="new answer")]}

        graph.graph = FakeGraph()

        first_dock = BottomInputDock()
        set_dock(first_dock)
        first_dock.begin_capture()
        try:
            await graph.run_turn("new question", context=TurnExecutionContext(thread_id=getattr(graph, "session_id", "") or "coding", session_id=getattr(graph, "session_id", "") or ""))
        finally:
            first_dock.deactivate()
            first_dock.reset()
            set_dock(None)

        rows = await load_transcript(session.id)
        assert {row.node_type for row in rows} >= {"turn", "thought", "tool_call", "tool_result"}
        assert any(row.tool_call_id == "call_read" for row in rows)

        second_dock = BottomInputDock()
        set_dock(second_dock)
        try:
            restored = await graph._restore_transcript_snapshot()
            rendered = "\n".join(second_dock.tree.render(120))

            assert restored is True
            assert "new question" in rendered
            assert "Thinking" in rendered
            assert "src/app.py" in rendered
        finally:
            second_dock.reset()
            set_dock(None)
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_run_turn_emits_turn_completed_event(tmp_path):
    session = await create_session(workspace=str(tmp_path))
    events: list[object] = []

    class RecordingConsumer:
        def handle(self, event):
            events.append(event)
            if isinstance(event, TurnStarted):
                return object()
            return None

    try:
        graph = _graph(tmp_path, session=session)

        class FakeGraph:
            async def astream(self, initial, _config, *, stream_mode="values"):
                yield {"messages": list(initial["messages"]) + [AIMessage(content="done")]}

        graph.graph = FakeGraph()

        test_dock = BottomInputDock()
        set_dock(test_dock)
        test_dock.begin_capture()
        ui_events.start(RecordingConsumer())
        try:
            await graph.run_turn("hello", context=TurnExecutionContext(thread_id=getattr(graph, "session_id", "") or "coding", session_id=getattr(graph, "session_id", "") or ""))
            await ui_events.drain()
        finally:
            await ui_events.stop()
            test_dock.deactivate()
            test_dock.reset()
            set_dock(None)

        assert any(isinstance(event, TurnCompleted) for event in events)
        assert not any(isinstance(event, TurnFailed) for event in events)
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_run_turn_emits_turn_failed_event_on_exception(tmp_path):
    session = await create_session(workspace=str(tmp_path))
    events: list[object] = []

    class RecordingConsumer:
        def handle(self, event):
            events.append(event)
            if isinstance(event, TurnStarted):
                return object()
            return None

    try:
        graph = _graph(tmp_path, session=session)

        class FakeGraph:
            async def astream(self, initial, _config, *, stream_mode="values"):
                if False:
                    yield
                raise RuntimeError("provider failed")

        graph.graph = FakeGraph()

        test_dock = BottomInputDock()
        set_dock(test_dock)
        test_dock.begin_capture()
        ui_events.start(RecordingConsumer())
        try:
            with pytest.raises(RuntimeError, match="provider failed"):
                await graph.run_turn("hello", context=TurnExecutionContext(thread_id=getattr(graph, "session_id", "") or "coding", session_id=getattr(graph, "session_id", "") or ""))
            await ui_events.drain()
        finally:
            await ui_events.stop()
            test_dock.deactivate()
            test_dock.reset()
            set_dock(None)

        failures = [event for event in events if isinstance(event, TurnFailed)]
        assert len(failures) == 1
        assert failures[0].message == "provider failed"
        assert not any(isinstance(event, TurnCompleted) for event in events)
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_run_turn_commits_event_todo_at_turn_end(tmp_path):
    session = await create_session(workspace=str(tmp_path))
    try:
        graph = _graph(tmp_path, session=session)

        class FakeGraph:
            async def astream(self, initial, _config, *, stream_mode="values"):
                from voidx.presentation.output.events import TodoItemPayload, TodoUpdated, ui_events

                await ui_events.emit(TodoUpdated(
                    items=[TodoItemPayload(id="review", content="finish review", status="done")],
                    summary="1/1 done · 0 active · 0 pending",
                ))
                yield {"messages": list(initial["messages"]) + [AIMessage(content="done")]}

        graph.graph = FakeGraph()

        test_dock = BottomInputDock()
        set_dock(test_dock)
        test_dock.begin_capture()
        ui_events.start(DockEventConsumer(test_dock))
        try:
            await graph.run_turn("track todo", context=TurnExecutionContext(thread_id=getattr(graph, "session_id", "") or "coding", session_id=getattr(graph, "session_id", "") or ""))
            await ui_events.drain()
        finally:
            await ui_events.stop()
            test_dock.deactivate()
            set_dock(None)

        todo_nodes = [node for node in test_dock.tree.root.children if node.node_type == "todo"]
        rows = await load_transcript(session.id)

        assert test_dock.todo_state() is None
        assert len(todo_nodes) == 1
        assert test_dock.tree.root.children[-1] is todo_nodes[0]
        assert todo_nodes[0].payload["summary"] == "1/1 done · 0 active · 0 pending"
        assert any(row.node_type == "todo" for row in rows)
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_run_turn_persists_todo_replay_rows(tmp_path):
    session = await create_session(workspace=str(tmp_path))
    try:
        graph = _graph(tmp_path, session=session)

        class FakeGraph:
            async def astream(self, initial, _config, *, stream_mode="values"):
                yield {
                    "messages": [
                        *list(initial["messages"]),
                        AIMessage(
                            content="",
                            tool_calls=[{
                                "name": "todo",
                                "args": {"todos": [{"content": "track work", "status": "active"}]},
                                "id": "call_todo",
                                "type": "tool_call",
                            }],
                        ),
                        AIMessage(content="done"),
                    ],
                }

        graph.graph = FakeGraph()

        test_dock = BottomInputDock()
        set_dock(test_dock)
        test_dock.begin_capture()
        try:
            await graph.run_turn("track todo", context=TurnExecutionContext(thread_id=getattr(graph, "session_id", "") or "coding", session_id=getattr(graph, "session_id", "") or ""))
        finally:
            test_dock.deactivate()
            set_dock(None)

        rows = await load_messages(session.id)

        assert [row.role for row in rows] == ["user", "assistant", "assistant"]
        assert rows[1].content == ""
        assert rows[2].content == "done"
        assert any(
            any(call.get("name") == "todo" for call in (row.tool_calls or []))
            for row in rows
        )
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_run_turn_persists_user_decision_tool_replay_rows(tmp_path):
    session = await create_session(workspace=str(tmp_path))
    try:
        graph = _graph(tmp_path, session=session)

        class FakeGraph:
            async def astream(self, initial, _config, *, stream_mode="values"):
                yield {
                    "messages": [
                        *list(initial["messages"]),
                        AIMessage(
                            content="",
                            tool_calls=[{
                                "name": "clarify",
                                "args": {"question": "Which scope?"},
                                "id": "call_clarify",
                                "type": "tool_call",
                            }],
                        ),
                        ToolMessage(content='{"answer": "frontend"}', tool_call_id="call_clarify"),
                        AIMessage(
                            content="",
                            tool_calls=[{
                                "name": "checkpoint",
                                "args": {"plan_summary": "Update frontend flow"},
                                "id": "call_plan",
                                "type": "tool_call",
                            }],
                        ),
                        ToolMessage(content='{"decision": "approved"}', tool_call_id="call_plan"),
                        AIMessage(content="done"),
                    ],
                }

        graph.graph = FakeGraph()

        test_dock = BottomInputDock()
        set_dock(test_dock)
        test_dock.begin_capture()
        try:
            await graph.run_turn("need a decision", context=TurnExecutionContext(thread_id=getattr(graph, "session_id", "") or "coding", session_id=getattr(graph, "session_id", "") or ""))
        finally:
            test_dock.deactivate()
            set_dock(None)

        rows = await load_messages(session.id)
        assistant_rows = [row for row in rows if row.role == "assistant"]
        tool_rows = [row for row in rows if row.role == "tool"]

        assert [call["name"] for row in assistant_rows for call in (row.tool_calls or [])] == [
            "clarify",
            "checkpoint",
        ]
        assert [row.tool_call_id for row in tool_rows] == ["call_clarify", "call_plan"]
        assert [row.content for row in tool_rows] == ['{"answer": "frontend"}', '{"decision": "approved"}']
    finally:
        await delete_session(session.id)

