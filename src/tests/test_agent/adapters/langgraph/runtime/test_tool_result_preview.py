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
from voidx.agent.domain.task.state import GoalResolution, GoalSpec, PlanResolution


from voidx.skills.context import SKILL_TOOL_CONTEXT_MARKER
from voidx.agent.application.automation.workflow.context import WORKFLOW_CONTEXT_MARKER
from voidx.agent.application.automation.workflow.runtime import WorkflowRunState, WorkflowRunStatus
from voidx.agent.domain.task.state import TaskState, ToolStatePatch

from voidx.agent.domain.automation.workflow import WorkflowRoute
from voidx.tooling.domain.context import ToolExecutionContext as ToolContext
from voidx.tooling.domain.result import ToolResult
from voidx.agent.adapters.tools.subagent import AgentResultContract, AgentTool
from voidx.tooling.builtin.file.search import FindTool
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
    from voidx.presentation.output.display_policy import ToolDisplayMode, ToolDisplayRule

    graph._display_policy = graph._display_policy.model_copy(
        update={
            "rules": {
                **graph._display_policy.rules,
                "agent": ToolDisplayRule(tool_name="agent", mode=ToolDisplayMode.SHOW),
            }
        }
    )
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



async def _execute_fake_tool(tmp_path, *, tool_name: str, args: dict, result: ToolResult):
    graph = _graph(tmp_path)

    class FakeTools:
        async def execute_tool(self, tid, _targs, _ctx):
            assert tid == tool_name
            return result

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
                "name": tool_name,
                "args": args,
                "id": "call_test",
                "type": "tool_call",
            }],
        )

        exec_res = await graph._execute_tools({
            "messages": [parent],
            "workspace": str(tmp_path),
            "persona": "voidx",
            "plan_mode": False,
        })
        await ui_events.drain()

        assistant = next(node for node in test_dock.tree.root.children if node.node_type == "assistant")
        tool_node = next(node for node in assistant.children if node.node_type == "tool_call")
        final_results = [
            node for node in (*tool_node.children, *assistant.children)
            if node.node_type == "tool_result"
        ]
        rendered = "\n".join(test_dock.tree.render(120))
        return rendered, final_results, list(exec_res["messages"])
    finally:
        await ui_events.stop()
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)





@pytest.mark.asyncio
async def test_generic_tool_does_not_output_result_in_ui_when_successful(tmp_path):
    bash_result = ToolResult(
        title="Bash: echo hello",
        output="hello",
        summary="ok",
        display="hello",
        metadata={"ok": True},
    )

    rendered, final_results, messages = await _execute_fake_tool(
        tmp_path,
        tool_name="bash",
        args={"command": "echo hello"},
        result=bash_result,
    )

    assert len(final_results) == 0
    assert not any(node.node_type == "tool_result" for node in final_results)
    assert any(isinstance(msg, ToolMessage) and msg.content == "hello" for msg in messages)


@pytest.mark.asyncio
async def test_failed_tool_does_not_output_error_in_ui(tmp_path):
    fail_result = ToolResult(
        title="Bash: exit 1",
        output="command failed: exit 1",
        summary="failed",
        display="command failed: exit 1",
        metadata={"ok": False, "error": True},
    )

    rendered, final_results, messages = await _execute_fake_tool(
        tmp_path,
        tool_name="bash",
        args={"command": "exit 1"},
        result=fail_result,
    )

    assert len(final_results) == 0
    assert "command failed: exit 1" not in rendered
    assert any(isinstance(msg, ToolMessage) and msg.content == "command failed: exit 1" for msg in messages)


@pytest.mark.asyncio
async def test_failed_read_tool_does_not_output_error_in_ui(tmp_path):
    fail_result = ToolResult(
        title="Read: nonexistent.py",
        output="File not found: nonexistent.py",
        summary="",
        metadata={"error": True},
    )

    rendered, final_results, messages = await _execute_fake_tool(
        tmp_path,
        tool_name="read",
        args={"file_path": "nonexistent.py"},
        result=fail_result,
    )

    assert len(final_results) == 0
    assert "File not found: nonexistent.py" not in rendered
    assert any(isinstance(msg, ToolMessage) and msg.content == "File not found: nonexistent.py" for msg in messages)


@pytest.mark.asyncio
async def test_find_invalid_arguments_does_not_render_in_ui(tmp_path):
    result = await FindTool().execute({}, ToolContext(workspace=str(tmp_path)))

    rendered, final_results, messages = await _execute_fake_tool(
        tmp_path,
        tool_name="find",
        args={},
        result=result,
    )

    assert len(final_results) == 0
    assert "Invalid arguments" not in rendered
    assert any(
        isinstance(message, ToolMessage)
        and "validation error for FindInput" in message.content
        for message in messages
    )


@pytest.mark.asyncio
async def test_direct_dock_path_renders_summary_without_raw_success_output(tmp_path):
    from voidx.agent.adapters.langgraph.runtime.tool_executor.ui import notify_tool_result
    from voidx.agent.domain.display_policy import DEFAULT_DISPLAY_RULES, ToolDisplayPolicy

    test_dock = BottomInputDock()
    test_dock.begin_capture()
    tool = test_dock.start_turn("read a file")
    tool = test_dock.start_tool(
        "Reading",
        'file_path="src/app.py"',
        tool_name="read",
        tool_call_id="read-direct",
        raw_args={"file_path": "src/app.py"},
    )
    result = ToolResult(
        title="Read: src/app.py",
        output="raw file contents must stay out of the TUI",
        summary="10/200 lines",
        metadata={"ok": True},
    )

    class _Ui:
        dock = test_dock
        ui = SimpleNamespace()

        @staticmethod
        def via_events():
            return False

        @staticmethod
        def title(tool_name):
            return tool_name

    try:
        await notify_tool_result(
            SimpleNamespace(_ui=_Ui()),
            {"name": "read", "id": "read-direct"},
            result,
            True,
            0.1,
            ToolDisplayPolicy.from_config({}, defaults=DEFAULT_DISPLAY_RULES),
            tool,
        )
        rendered = "\n".join(test_dock.tree.render(120))
        assert "10/200 lines" in rendered
        assert "raw file contents must stay out of the TUI" not in rendered
    finally:
        test_dock.deactivate()
        test_dock.reset()
