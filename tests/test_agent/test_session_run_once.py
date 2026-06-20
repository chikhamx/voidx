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
from voidx.runtime import GoalResolution, GoalSpec, GoalType, IntentResolution, PlanResolution, TaskIntent
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
    goal_type: GoalType = GoalType.FEATURE,
    *,
    desc: str = "Implement the feature",
    join: str = "tdd",
    leave: str = "verify",
) -> GoalResolution:
    return GoalResolution(
        intent=IntentResolution(type=TaskIntent.CODING, desc="delegated child task"),
        goal=GoalSpec(type=goal_type, desc=desc),
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
    goal_type: GoalType = GoalType.INSPECT,
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
async def test_run_once_persists_and_restores_transcript_snapshot(tmp_path):
    session = await create_session(workspace=str(tmp_path))
    try:
        graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None, session=session)

        class FakeGraph:
            async def ainvoke(self, initial, _config):
                from voidx.ui.output.dock import dock

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
                return {"messages": list(initial["messages"]) + [AIMessage(content="new answer")]}

        graph.graph = FakeGraph()

        first_dock = BottomInputDock()
        set_dock(first_dock)
        first_dock.begin_capture()
        try:
            await graph._run_once("new question")
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
async def test_run_once_commits_event_todo_at_turn_end(tmp_path):
    session = await create_session(workspace=str(tmp_path))
    try:
        graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None, session=session)

        class FakeGraph:
            async def ainvoke(self, initial, _config):
                from voidx.ui.output.events import TodoItemPayload, TodoUpdated, ui_events

                await ui_events.emit(TodoUpdated(
                    items=[TodoItemPayload(content="finish review", status="completed")],
                    summary="1/1 done · 0 active · 0 pending",
                ))
                return {"messages": list(initial["messages"]) + [AIMessage(content="done")]}

        graph.graph = FakeGraph()

        test_dock = BottomInputDock()
        set_dock(test_dock)
        test_dock.begin_capture()
        ui_events.start(DockEventConsumer(test_dock))
        try:
            await graph._run_once("track todo")
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
async def test_run_once_persists_sanitized_todo_replay_rows(tmp_path):
    session = await create_session(workspace=str(tmp_path))
    try:
        graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None, session=session)

        class FakeGraph:
            async def ainvoke(self, initial, _config):
                return {
                    "messages": [
                        *list(initial["messages"]),
                        AIMessage(
                            content="",
                            tool_calls=[{
                                "name": "todo",
                                "args": {"todos": [{"content": "track work", "status": "in_progress"}]},
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
            await graph._run_once("track todo")
        finally:
            test_dock.deactivate()
            set_dock(None)

        rows = await load_messages(session.id)

        assert [row.role for row in rows] == ["user", "assistant"]
        assert rows[1].content == "done"
        assert all(
            not any(call.get("name") == "todo" for call in (row.tool_calls or []))
            for row in rows
        )
        assert all(row.tool_call_id != "call_todo" for row in rows)
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_run_once_persists_user_decision_tool_replay_rows(tmp_path):
    session = await create_session(workspace=str(tmp_path))
    try:
        graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None, session=session)

        class FakeGraph:
            async def ainvoke(self, initial, _config):
                return {
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
            await graph._run_once("need a decision")
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


