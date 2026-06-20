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


def test_agent_tool_result_preview_preserves_short_output():
    assert _agent_result_preview("short child conclusion\nsecond line") == "short child conclusion\nsecond line"


def test_agent_tool_result_preview_omits_extra_lines():
    output = "\n".join(f"child result line {index}" for index in range(1, 8))

    preview = _agent_result_preview(output)

    assert "child result line 1" in preview
    assert "child result line 5" in preview
    assert "child result line 6" not in preview
    assert "child result line 7" not in preview
    assert "... (2 more lines omitted; full result passed to voidx)" in preview


def test_agent_tool_result_preview_caps_long_single_line():
    output = "x" * (AGENT_RESULT_PREVIEW_CHARS + 17)

    preview = _agent_result_preview(output)

    assert preview.startswith("x" * AGENT_RESULT_PREVIEW_CHARS)
    assert len(preview.splitlines()[0]) == AGENT_RESULT_PREVIEW_CHARS
    assert "... (17 more chars omitted; full result passed to voidx)" in preview


async def _execute_fake_agent_tool_with_output(tmp_path, output: str, *, debug: bool = False):
    graph = _graph(tmp_path)
    graph.set_debug(debug)

    class FakeTools:
        async def execute_tool(self, tid, _targs, _ctx):
            assert tid == "agent"
            return ToolResult(output=output)

    async def allow_all(
        tool_calls,
        plan_mode: bool,
        session_id: str,
        interaction_mode=None,
    ):
        return tool_calls, []

    graph.tools = FakeTools()
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
                "args": {"agent": "explore", "description": "inspect auth flow"},
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
        agent_tool = next(node for node in assistant.children if node.node_type == "tool_call")
        final_results = [node for node in agent_tool.children if node.node_type == "tool_result"]
        final_texts = ["\n".join([node.header, *node.body_lines]) for node in final_results]
        rendered = "\n".join(test_dock.tree.render(120))
        return rendered, final_texts, list(result["messages"])
    finally:
        graph.set_debug(False)
        await ui_events.stop()
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)


@pytest.mark.asyncio
async def test_agent_tool_result_previewed_in_ui(tmp_path):
    output = "\n".join(f"child final line {index}" for index in range(1, 8))

    rendered, final_texts, messages = await _execute_fake_agent_tool_with_output(tmp_path, output)

    assert len(final_texts) == 1
    assert "child final line 1" in final_texts[0]
    assert "child final line 5" in final_texts[0]
    assert "child final line 6" not in final_texts[0]
    assert "child final line 7" not in rendered
    assert "... (2 more lines omitted; full result passed to voidx)" in final_texts[0]
    assert any(isinstance(message, ToolMessage) and message.content == output for message in messages)


@pytest.mark.asyncio
async def test_agent_tool_result_preview_does_not_depend_on_debug(tmp_path):
    output = "\n".join(f"debug child line {index}" for index in range(1, 8))

    _rendered, final_texts, _messages = await _execute_fake_agent_tool_with_output(
        tmp_path,
        output,
        debug=True,
    )

    assert len(final_texts) == 1
    assert "debug child line 5" in final_texts[0]
    assert "debug child line 6" not in final_texts[0]
    assert "... (2 more lines omitted; full result passed to voidx)" in final_texts[0]


