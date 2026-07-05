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
async def test_execute_tools_does_not_apply_removed_on_intent_state_patch(tmp_path):
    graph = _graph(tmp_path)

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
        tool_calls=[{"name": "on_intent", "args": {}, "id": "call_intent", "type": "tool_call"}],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "coordinate",
        "plan_mode": False,
        "interaction_mode": "auto",
        "task_state": _task_state_json(current_intent=TaskIntent.GENERAL),
    })

    assert "task_state" not in result
    assert result["messages"][0].tool_call_id == "call_intent"
    assert "Unknown tool: on_intent" in result["messages"][0].content


@pytest.mark.asyncio
async def test_compact_context_tool_applies_inline_summary_and_replaces_live_messages(tmp_path):
    graph = _graph(tmp_path)
    persisted: dict[str, object] = {}

    graph._compaction.select_details = lambda messages: CompactionSelection(
        head=messages[:2],
        tail_id=getattr(messages[2], "id", None),
        keep_from=2,
        mode="normal",
    )

    async def persist(head_messages):
        persisted["head"] = list(head_messages)

    graph._persist_compaction = persist

    async def allow_all(tool_calls, **_kwargs):
        return tool_calls, []

    graph._authorize_tool_calls = allow_all
    parent = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "compact",
                "args": {"summary": "inline summary", "tail_anchor_id": "current_user"},
                "id": "call_compact",
                "type": "tool_call",
            }
        ],
    )

    result = await graph._execute_tools({
        "messages": [
            HumanMessage(content="older question", id="older_user"),
            AIMessage(content="older answer", id="older_assistant"),
            HumanMessage(content="current question", id="current_user"),
            parent,
        ],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
        "interaction_mode": "auto",
        "task_state": _task_state_json(),
    })

    messages = result["messages"]
    assert isinstance(messages[0], RemoveMessage)
    assert messages[0].id == REMOVE_ALL_MESSAGES
    assert [message.content for message in messages[1:]] == [
        "## Long Summary\ninline summary",
        "current question",
        "",
        "Compacted older context into the runtime summary.",
    ]
    assert graph._pending_summary == "inline summary"
    assert graph._compaction_summary == "inline summary"
    assert [message.content for message in persisted["head"]] == ["older question", "older answer"]


@pytest.mark.asyncio
async def test_plan_checkpoint_transaction_executes_following_tools_with_updated_state(tmp_path):
    graph = _graph(tmp_path)
    observed: dict[str, object] = {}

    class RecordingReadTool:
        id = "read"
        description = "fake read"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            observed["task_intent"] = ctx.task_intent
            observed["goal_target"] = ctx.goal_target
            observed["goal_type"] = ctx.goal_type
            observed["workflow_turns"] = {
                run.name: (run.status.value, run.updated_turn)
                for run in ctx.workflow_runs
            }
            return ToolResult(output=f"read after plan: {ctx.task_intent}:{ctx.goal_type}:{ctx.goal_target}")

    graph.tools.register("read", RecordingReadTool(), "fake read", {"type": "object", "properties": {}})

    async def allow_all(
        tool_calls,
        plan_mode: bool,
        session_id: str,
        interaction_mode=None,
    ):
        return tool_calls, []

    class FakeApp:
        async def ask_choice(self, prompt, choices, **kwargs):
            return "approved"

        async def ask_text(self, prompt, **kwargs):
            return ""

    graph._authorize_tool_calls = allow_all
    graph._app = FakeApp()
    parent = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "checkpoint",
                "args": {"plan_summary": "Update runtime state handling"},
                "id": "call_plan",
                "type": "tool_call",
            },
            {
                "name": "read",
                "args": {"file_path": "src/app.py"},
                "id": "call_read",
                "type": "tool_call",
            },
        ],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
        "interaction_mode": "auto",
        "step_count": 5,
        "task_state": _task_state_json(
            current_intent=TaskIntent.CODING,
            workflow_runs={
                "debug": WorkflowRunState(name="debug", status=WorkflowRunStatus.ACTIVE),
            },
        ),
    })

    assert [message.tool_call_id for message in result["messages"]] == ["call_plan", "call_read"]
    task_state = _result_task_state(result)
    assert task_state.current_intent == TaskIntent.CODING
    assert task_state.current_goal is not None
    assert task_state.current_goal.desc == "Update runtime state handling"
    assert result["messages"][1].content == "read after plan: coding:feature:Update runtime state handling"
    assert observed == {
        "task_intent": "coding",
        "goal_type": "feature",
        "goal_target": "Update runtime state handling",
        "workflow_turns": {
            "debug": ("satisfied", 5),
            "tdd": ("active", 5),
        },
    }
