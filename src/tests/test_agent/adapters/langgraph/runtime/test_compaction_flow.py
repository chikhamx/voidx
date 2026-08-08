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
        "should_continue": True,
    }

    await graph._prepare_with_stream(state)

    assert isinstance(messages[0], SystemMessage)
    assert "Long Summary" in messages[0].content
    assert "summary text" in messages[0].content
    assert "You are voidx" in messages[0].content


@pytest.mark.asyncio
async def test_compaction_progress_status_updates_are_record_only(tmp_path):
    graph = _graph(tmp_path)
    events = []

    class Recorder:
        def via_events(self):
            return True

        class Events:
            async def emit(self_inner, event):
                events.append(event)

        events = Events()

    graph._ui = Recorder()
    graph._compaction.is_overflow = lambda _tokens: True
    graph._compaction.select_details = lambda messages: CompactionSelection(
        head=messages[:2],
        tail_id=getattr(messages[2], "id", None),
        keep_from=2,
        mode="full",
    )

    async def summarize(_head_messages, _previous_summary):
        return "summary text"

    async def persist(_head_messages):
        return None

    await graph._compaction_component().compact_for_live_state(
        [
            HumanMessage(content="older question", id="older_user"),
            AIMessage(content="older answer"),
            HumanMessage(content="current question", id="current_user"),
        ],
        run_compaction_agent=summarize,
        persist_compaction=persist,
    )

    compaction_updates = [
        event for event in events
        if isinstance(event, StatusUpdated) and event.status_id == "compaction"
    ]
    assert compaction_updates
    assert all(event.display == "record_only" for event in compaction_updates)




@pytest.mark.asyncio
async def test_preflight_compaction_returns_structured_metadata(tmp_path):
    graph = _graph(tmp_path)
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

    graph._run_compaction_agent = summarize
    messages = [
        HumanMessage(content="older question", id="older_user"),
        AIMessage(content="older answer"),
        HumanMessage(content="previous question", id="previous_user"),
        AIMessage(content="previous answer"),
        HumanMessage(content="current question", id="current_user"),
    ]

    result, preflight = await graph._preflight_compact_if_needed(
        messages,
        [],
        reason="soft_threshold",
    )

    assert result is not None
    assert preflight.compacted is True
    assert preflight.summary == "summary text"
    assert preflight.removed_message_count == 2
    assert preflight.tail_anchor_id == "previous_user"
    assert preflight.reason == "soft_threshold"
    assert preflight.post_target_tokens == graph._compaction.post_compaction_target()
    assert result.live_messages[-1].content == "current question"
@pytest.mark.asyncio
async def test_compaction_asks_only_when_configured_and_can_skip(tmp_path):
    graph = make_langgraph_execution(Config(workspace=str(tmp_path), ask_compact=True), api_key=None)
    graph._compaction.is_overflow = lambda _tokens: True
    asked: list[str] = []

    class FakeApp:
        async def ask_choice(self, prompt, choices, details=None):
            asked.append(prompt)
            assert [choice[1] for choice in choices] == ["compact", "skip"]
            return "skip"

    async def fail_if_compacted(_head_messages, _previous_summary):
        pytest.fail("skip once should not run compaction")

    graph._ui.bind_frontend( FakeApp())
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
    graph = make_langgraph_execution(Config(workspace=str(tmp_path)), api_key=None)
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

    graph._ui.bind_frontend( FakeApp())
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
    graph = make_langgraph_execution(Config(workspace=str(tmp_path)), api_key=None)
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
async def test_preflight_compaction_uses_soft_threshold_and_target_tail(tmp_path):
    graph = make_langgraph_execution(Config(workspace=str(tmp_path)), api_key=None)
    graph._compaction.is_overflow = lambda _tokens: False
    graph._compaction.is_soft_overflow = lambda _tokens: True
    graph._compaction.soft_threshold = lambda: 90
    graph._compaction.post_compaction_target = lambda: 20
    used_preflight_selection: list[bool] = []

    def select_preflight(messages, *, model=""):
        used_preflight_selection.append(True)
        return CompactionSelection(
            head=messages[:2],
            tail_id=getattr(messages[2], "id", None),
            keep_from=2,
            mode="normal",
        )

    graph._compaction.select_preflight_details = select_preflight

    async def summarize(_head_messages, _previous_summary):
        return "preflight summary"

    async def persist(_head_messages):
        return None

    messages = [
        HumanMessage(content="old question", id="old_user"),
        AIMessage(content="old answer", id="old_assistant"),
        HumanMessage(content="previous question", id="previous_user"),
        AIMessage(content="previous answer", id="previous_assistant"),
        HumanMessage(content="current question", id="current_user"),
    ]

    result = await graph._compaction_component().compact_for_live_state(
        messages,
        preflight=True,
        ask=False,
        run_compaction_agent=summarize,
        persist_compaction=persist,
    )

    assert used_preflight_selection == [True]
    assert result is not None
    assert [message.content for message in result.removed_messages] == [
        "old question",
        "old answer",
    ]
    assert [message.content for message in result.live_messages] == [
        "previous question",
        "previous answer",
        "current question",
    ]
    assert result.tail_id == "previous_user"
    assert result.metadata["compaction_reason"] == "soft_threshold"
    assert result.metadata["soft_threshold"] == 90
    assert result.metadata["post_compaction_target"] == 20
    assert result.metadata["removed_message_count"] == 2
    assert result.metadata["retained_turn_count"] == 2
    assert result.metadata["current_user_preserved"] is True
    assert result.metadata["inline_compaction_enabled"] is False


@pytest.mark.asyncio
async def test_maybe_compact_preflight_preserves_current_user_message(tmp_path):
    graph = make_langgraph_execution(Config(workspace=str(tmp_path)), api_key=None)
    graph._compaction.is_overflow = lambda _tokens: False
    graph._compaction.is_soft_overflow = lambda _tokens: True
    graph._compaction.select_preflight_details = lambda messages, *, model="": CompactionSelection(
        head=messages[:2],
        tail_id=getattr(messages[2], "id", None),
        keep_from=2,
        mode="normal",
    )

    async def summarize(_head_messages, _previous_summary):
        return "preflight summary"

    graph._run_compaction_agent = summarize
    messages = [
        HumanMessage(content="old question", id="old_user"),
        AIMessage(content="old answer", id="old_assistant"),
        HumanMessage(content="previous question", id="previous_user"),
        AIMessage(content="previous answer", id="previous_assistant"),
        HumanMessage(content="current question", id="current_user"),
    ]

    removed, tail_id = await graph._maybe_compact(messages, [], ask=False, preflight=True)

    assert [message.content for message in removed or []] == ["old question", "old answer"]
    assert tail_id == "previous_user"
    assert [message.content for message in messages] == [
        "previous question",
        "previous answer",
        "current question",
    ]
