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
from voidx.presentation.output.events import DockEventConsumer, StatusFinished, StatusUpdated, TurnStarted, ui_events


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



async def test_compaction_uses_previous_summary_and_prunes_persisted_head(tmp_path):
    session = await create_session(workspace=str(tmp_path))
    try:
        await save_message(MessageRow(session_id=session.id, role="user", content="old question"))
        await save_message(MessageRow(session_id=session.id, role="assistant", content="old answer"))
        await save_message(MessageRow(session_id=session.id, role="user", content="tail question"))

        graph = make_langgraph_execution(Config(workspace=str(tmp_path)), api_key=None, session=session)
        graph._compaction_summary = "previous summary"
        graph._compaction.is_overflow = lambda _tokens: True
        graph._compaction.select_details = lambda messages: CompactionSelection(
            head=messages[:2],
            tail_id=getattr(messages[2], "id", None),
            keep_from=2,
            mode="normal",
        )
        captured: dict[str, object] = {}

        async def summarize(_head_messages, previous_summary):
            captured["previous"] = previous_summary
            return "updated summary"

        class FakeGraph:
            async def astream(self, initial, _config, *, stream_mode="values"):
                captured["initial_contents"] = [
                    str(getattr(message, "content", ""))
                    for message in initial["messages"]
                ]
                yield {"messages": list(initial["messages"]) + [AIMessage(content="new answer")]}

        graph._run_compaction_agent = summarize
        graph.graph = FakeGraph()

        test_dock = BottomInputDock()
        set_dock(test_dock)
        test_dock.begin_capture()
        try:
            await graph.run_turn("current question", context=TurnExecutionContext(thread_id=getattr(graph, "session_id", "") or "coding", session_id=getattr(graph, "session_id", "") or ""))
        finally:
            test_dock.deactivate()
            test_dock.reset()
            set_dock(None)

        rows = await load_messages(session.id)
        contents = [row.content for row in rows]
        initial_contents = captured["initial_contents"]
        assert captured["previous"] == "previous summary"
        assert "old question" not in contents
        assert "old answer" not in contents
        assert "tail question" in contents
        assert "current question" in contents
        assert "old question" not in initial_contents
        assert "old answer" not in initial_contents
        assert "tail question" in initial_contents
        assert "current question" in initial_contents
        assert graph._compaction_summary == "updated summary"

        resumed = make_langgraph_execution(Config(workspace=str(tmp_path)), api_key=None, session=session)
        await resumed.restore_runtime_state()

        assert resumed._compaction_summary == "updated summary"
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_run_turn_passes_compacted_messages_to_graph(tmp_path):
    session = await create_session(workspace=str(tmp_path))
    try:
        await save_message(MessageRow(session_id=session.id, role="user", content="old question"))
        await save_message(MessageRow(session_id=session.id, role="assistant", content="old answer"))
        await save_message(MessageRow(session_id=session.id, role="user", content="tail question"))

        graph = make_langgraph_execution(Config(workspace=str(tmp_path)), api_key=None, session=session)
        graph._compaction.is_overflow = lambda _tokens: False
        graph._compaction.is_soft_overflow = lambda _tokens: True
        graph._compaction.select_preflight_details = lambda messages, *, model="": CompactionSelection(
            head=messages[:2],
            tail_id=getattr(messages[2], "id", None),
            keep_from=2,
            mode="normal",
        )

        async def summarize(_head_messages, _previous_summary):
            return "summary text"

        captured: dict[str, list[str]] = {}

        class FakeGraph:
            async def astream(self, initial, _config, *, stream_mode="values"):
                captured["messages"] = [
                    str(getattr(message, "content", ""))
                    for message in initial["messages"]
                ]
                yield {"messages": list(initial["messages"]) + [AIMessage(content="new answer")]}

        graph._run_compaction_agent = summarize
        graph.graph = FakeGraph()

        test_dock = BottomInputDock()
        set_dock(test_dock)
        test_dock.begin_capture()
        try:
            await graph.run_turn("current question", context=TurnExecutionContext(thread_id=getattr(graph, "session_id", "") or "coding", session_id=getattr(graph, "session_id", "") or ""))
        finally:
            test_dock.deactivate()
            test_dock.reset()
            set_dock(None)

        assert "old question" not in captured["messages"]
        assert "old answer" not in captured["messages"]
        assert "tail question" in captured["messages"]
        assert "current question" in captured["messages"]
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_run_turn_finishes_analyzing_before_preflight_compaction_status(tmp_path):
    session = await create_session(workspace=str(tmp_path))
    try:
        await save_message(MessageRow(session_id=session.id, role="user", content="old question"))
        await save_message(MessageRow(session_id=session.id, role="assistant", content="old answer"))
        await save_message(MessageRow(session_id=session.id, role="user", content="tail question"))

        graph = make_langgraph_execution(Config(workspace=str(tmp_path)), api_key=None, session=session)
        graph._compaction.is_overflow = lambda _tokens: False
        graph._compaction.is_soft_overflow = lambda _tokens: True
        graph._compaction.select_preflight_details = lambda messages, *, model="": CompactionSelection(
            head=messages[:2],
            tail_id=getattr(messages[2], "id", None),
            keep_from=2,
            mode="normal",
        )

        async def summarize(_head_messages, _previous_summary):
            return "summary text"

        class FakeGraph:
            async def astream(self, initial, _config, *, stream_mode="values"):
                yield {"messages": list(initial["messages"]) + [AIMessage(content="new answer")]}

        graph._run_compaction_agent = summarize
        graph.graph = FakeGraph()

        events = []

        class Recorder:
            def handle(self, event):
                events.append(event)
                return None

        test_dock = BottomInputDock()
        set_dock(test_dock)
        test_dock.begin_capture()
        ui_events.start(Recorder())
        try:
            await graph.run_turn("current question", context=TurnExecutionContext(thread_id=getattr(graph, "session_id", "") or "coding", session_id=getattr(graph, "session_id", "") or ""))
            await ui_events.drain()
        finally:
            await ui_events.stop()
            test_dock.deactivate()
            test_dock.reset()
            set_dock(None)

        analyzing_finish_index = next(
            index
            for index, event in enumerate(events)
            if isinstance(event, StatusFinished) and event.status_id == "turn:analyzing"
        )
        compaction_update_index = next(
            index
            for index, event in enumerate(events)
            if isinstance(event, StatusUpdated) and event.status_id == "compaction"
        )
        assert analyzing_finish_index < compaction_update_index
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_compaction_drops_removed_row_cache_entries(tmp_path):
    session = await create_session(workspace=str(tmp_path))
    try:
        await save_message(MessageRow(session_id=session.id, role="user", content="old question"))
        await save_message(MessageRow(session_id=session.id, role="assistant", content="old answer"))
        await save_message(MessageRow(session_id=session.id, role="user", content="tail question"))

        graph = make_langgraph_execution(Config(workspace=str(tmp_path)), api_key=None, session=session)
        graph._context_cache.row_messages = {
            1: RowMessageCacheEntry("old-user", HumanMessage(content="old question", id="1")),
            2: RowMessageCacheEntry("old-assistant", AIMessage(content="old answer", id="2")),
            3: RowMessageCacheEntry("tail-user", HumanMessage(content="tail question", id="3")),
        }

        await graph._persist_compaction([
            HumanMessage(content="old question", id="1"),
            AIMessage(content="old answer", id="2"),
        ])

        assert set(graph._context_cache.row_messages) == {3}
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_slash_compact_runs_manual_session_compaction(tmp_path):
    from voidx.agent.slash import SlashHandler

    session = await create_session(workspace=str(tmp_path))
    try:
        await save_message(MessageRow(session_id=session.id, role="user", content="old question"))
        await save_message(MessageRow(session_id=session.id, role="assistant", content="old answer"))
        await save_message(MessageRow(session_id=session.id, role="user", content="tail question"))

        graph = make_langgraph_execution(Config(workspace=str(tmp_path), ask_compact=True), api_key=None, session=session)
        graph._compaction.select_details = lambda messages: CompactionSelection(
            head=messages[:2],
            tail_id=getattr(messages[2], "id", None),
            keep_from=2,
            mode="normal",
        )

        async def summarize(_head_messages, _previous_summary):
            return "manual summary"

        graph._run_compaction_agent = summarize

        handled = await SlashHandler(graph).dispatch("/compact")

        rows = await load_messages(session.id)
        assert handled is True
        assert [row.content for row in rows] == ["tail question"]
        assert graph._compaction_summary == "manual summary"
    finally:
        await delete_session(session.id)
