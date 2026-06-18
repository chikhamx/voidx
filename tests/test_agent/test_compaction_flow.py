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
    step_budget: int = 4,
) -> dict:
    return {
        "goal_resolution": _child_goal_resolution(goal_type, desc=desc, join=join, leave=leave),
        "result_contract": _child_result_contract(schema_name),
        "step_budget": step_budget,
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
async def test_compaction_trims_head_and_injects_summary_into_system_prompt(tmp_path):
    graph = _graph(tmp_path)
    graph._compaction.is_overflow = lambda _tokens: True
    graph._compaction.select_details = lambda messages: CompactionSelection(
        head=messages[:2],
        tail_id=getattr(messages[2], "id", None),
        keep_from=2,
        mode="full",
    )

    async def summarize(_head_messages, _previous_summary):
        return "summary text"

    graph._run_compaction_agent = summarize
    messages = [
        HumanMessage(content="older question", id="older_user"),
        AIMessage(content="older answer"),
        HumanMessage(content="old question", id="old_user"),
        AIMessage(content="old answer"),
        HumanMessage(content="current question", id="current_user"),
    ]

    await graph._maybe_compact(messages, [])

    assert len(messages) == 3
    assert isinstance(messages[0], HumanMessage)
    assert messages[0].content == "old question"
    assert messages[-1].content == "current question"
    assert graph._pending_summary == "summary text"

    state = {
        "messages": messages,
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
        "tool_results": {},
        "step_count": 0,
        "max_steps": 50,
        "should_continue": True,
    }

    await graph._prepare_with_stream(state)

    assert isinstance(messages[0], SystemMessage)
    assert "Long Summary" in messages[0].content
    assert "summary text" in messages[0].content
    assert "You are voidx" in messages[0].content


@pytest.mark.asyncio
async def test_compaction_asks_only_when_configured_and_can_skip(tmp_path):
    graph = VoidXGraph(Config(workspace=str(tmp_path), ask_compact=True), api_key=None)
    graph._compaction.is_overflow = lambda _tokens: True
    asked: list[str] = []

    class FakeApp:
        async def ask_choice(self, prompt, choices, details=None):
            asked.append(prompt)
            assert [choice[1] for choice in choices] == ["compact", "skip"]
            return "skip"

    async def fail_if_compacted(_head_messages, _previous_summary):
        pytest.fail("skip once should not run compaction")

    graph._app = FakeApp()
    graph._run_compaction_agent = fail_if_compacted
    messages = [
        HumanMessage(content="old question", id="1"),
        AIMessage(content="old answer", id="2"),
        HumanMessage(content="current question", id="3"),
    ]

    await graph._maybe_compact(messages, [])

    assert asked == ["Compact context?"]
    assert [message.content for message in messages] == ["old question", "old answer", "current question"]
    assert graph._pending_summary is None


@pytest.mark.asyncio
async def test_compaction_auto_compacts_by_default_without_asking(tmp_path):
    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None)
    graph._compaction.is_overflow = lambda _tokens: True
    graph._compaction.select_details = lambda messages: CompactionSelection(
        head=messages[:2],
        tail_id=getattr(messages[2], "id", None),
        keep_from=2,
        mode="full",
    )

    class FakeApp:
        async def ask_choice(self, _prompt, _choices, details=None):
            pytest.fail("default compaction should not ask")

    async def summarize(_head_messages, _previous_summary):
        return "auto summary"

    graph._app = FakeApp()
    graph._run_compaction_agent = summarize
    messages = [
        HumanMessage(content="older question", id="0"),
        AIMessage(content="older answer"),
        HumanMessage(content="old question", id="1"),
        AIMessage(content="old answer", id="2"),
        HumanMessage(content="current question", id="3"),
    ]

    await graph._maybe_compact(messages, [])

    assert [message.content for message in messages] == ["old question", "old answer", "current question"]
    assert graph._pending_summary == "auto summary"
    assert graph._compaction_summary == "auto summary"


@pytest.mark.asyncio
async def test_compaction_fallback_returns_removed_messages(tmp_path):
    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None)
    graph._compaction.is_overflow = lambda _tokens: True
    graph._compaction.select_details = lambda messages: CompactionSelection(
        head=messages[:2],
        tail_id=getattr(messages[2], "id", None),
        keep_from=2,
        mode="full",
    )
    messages = [
        HumanMessage(content="old 1", id="1"),
        AIMessage(content="old 2"),
        HumanMessage(content="tail 1", id="2"),
        AIMessage(content="tail 2"),
        HumanMessage(content="current", id="3"),
    ]

    removed, tail_id = await graph._maybe_compact(messages, [], ask=False)

    assert [message.content for message in messages] == [
        "tail 1",
        "tail 2",
        "current",
    ]
    assert [message.content for message in removed or []] == ["old 1", "old 2"]
    assert tail_id == "2"
    assert "old 1" in graph._pending_summary
    assert "old 2" in graph._pending_summary


@pytest.mark.asyncio
async def test_compaction_uses_previous_summary_and_prunes_persisted_head(tmp_path):
    session = await create_session(workspace=str(tmp_path))
    try:
        await save_message(MessageRow(session_id=session.id, role="user", content="old question"))
        await save_message(MessageRow(session_id=session.id, role="assistant", content="old answer"))
        await save_message(MessageRow(session_id=session.id, role="user", content="tail question"))

        graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None, session=session)
        graph._compaction_summary = "previous summary"
        graph._compaction.is_overflow = lambda _tokens: True
        graph._compaction.select_details = lambda messages: CompactionSelection(
            head=messages[:2],
            tail_id=getattr(messages[2], "id", None),
            keep_from=2,
            mode="normal",
        )
        captured: dict[str, str | None] = {}

        async def summarize(_head_messages, previous_summary):
            captured["previous"] = previous_summary
            return "updated summary"

        class FakeGraph:
            async def ainvoke(self, initial, _config):
                return {"messages": list(initial["messages"]) + [AIMessage(content="new answer")]}

        graph._run_compaction_agent = summarize
        graph.graph = FakeGraph()

        test_dock = BottomInputDock()
        set_dock(test_dock)
        test_dock.begin_capture()
        try:
            await graph._run_once("current question")
        finally:
            test_dock.deactivate()
            test_dock.reset()
            set_dock(None)

        rows = await load_messages(session.id)
        contents = [row.content for row in rows]
        assert captured["previous"] == "previous summary"
        assert "old question" not in contents
        assert "old answer" not in contents
        assert "tail question" in contents
        assert "current question" in contents
        assert graph._compaction_summary == "updated summary"

        resumed = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None, session=session)
        await resumed._restore_runtime_state()

        assert resumed._compaction_summary == "updated summary"
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_compaction_drops_removed_row_cache_entries(tmp_path):
    session = await create_session(workspace=str(tmp_path))
    try:
        await save_message(MessageRow(session_id=session.id, role="user", content="old question"))
        await save_message(MessageRow(session_id=session.id, role="assistant", content="old answer"))
        await save_message(MessageRow(session_id=session.id, role="user", content="tail question"))

        graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None, session=session)
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

        graph = VoidXGraph(Config(workspace=str(tmp_path), ask_compact=True), api_key=None, session=session)
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


