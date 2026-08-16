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
async def test_graph_on_request_auto_approves_need_ask_tools(tmp_path):
    graph = _graph(tmp_path)
    graph._permission.permission_mode = "full_access"

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "write", "args": {"file_path": "app.py", "op": "append", "new_string": "x"}, "id": "call_1"}],
        runtime_persona="implement",
        plan_mode=False,
        session_id="test",
    )

    assert [tc["name"] for tc in approved] == ["write"]
    assert denied == []


@pytest.mark.asyncio
async def test_full_access_auto_approves_write_without_implement_persona(tmp_path):
    graph = _graph(tmp_path)
    graph._permission.permission_mode = "full_access"

    async def fail_if_asked(_tool_calls):
        pytest.fail("full_access should not prompt for workspace edit")

    graph._ask_tool_permission = fail_if_asked

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "replace", "args": {"file_path": "app.py", "bounds": [{"line_no": 1, "anchor": "x"}], "new_string": "y"}, "id": "call_1"}],
        runtime_persona="coordinate",
        plan_mode=False,
        session_id="test",
    )

    assert [tc["name"] for tc in approved] == ["replace"]
    assert denied == []


@pytest.mark.asyncio
async def test_graph_on_failure_asks_unsafe_bash_before_prompt(tmp_path):
    graph = _graph(tmp_path)
    
    asked: list[dict] = []

    async def ask(tool_calls):
        asked.extend(tool_calls)
        return "y"

    graph._ask_tool_permission = ask
    approved, denied = await graph._authorize_tool_calls(
        [{"name": "bash", "args": {"command": "pip install requests"}, "id": "call_1"}],
        plan_mode=False,
        session_id="test",
    )

    assert len(approved) == 1
    assert denied == []
    assert len(asked) == 1


def test_tool_result_ok_detects_structured_failures():
    from voidx.agent.adapters.langgraph.execution import LangGraphExecution

    assert LangGraphExecution._tool_result_ok(ToolResult(output="ok", metadata={"exit_code": 0}))
    assert not LangGraphExecution._tool_result_ok(ToolResult(output="failed", metadata={"exit_code": 2}))
    assert not LangGraphExecution._tool_result_ok(ToolResult(output="blocked", metadata={"blocked": True}))
    assert not LangGraphExecution._tool_result_ok(ToolResult(output="error", metadata={"error": True}))


@pytest.mark.asyncio
async def test_execute_tools_includes_next_step_hint_in_llm_message(tmp_path):
    graph = _graph(tmp_path)

    class FakeTools:
        async def execute_tool(self, tid, _targs, _ctx):
            assert tid == "checkpoint"
            return ToolResult(
                output='{"decision": "needs_doc"}',
                next_step_hint="Plan approved with doc request. Write a design document before implementing.",
            )

    async def allow_all(tool_calls, **_kwargs):
        return tool_calls, []

    graph.tools = FakeTools()
    graph._authorize_tool_calls = allow_all

    parent = AIMessage(
        content="",
        tool_calls=[{
            "name": "checkpoint",
            "args": {"goal": "Add docs"},
            "id": "call_checkpoint",
            "type": "tool_call",
        }],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
    })

    tool_messages = [msg for msg in result["messages"] if isinstance(msg, ToolMessage)]
    assert tool_messages[0].content.startswith('{"decision": "needs_doc"}')
    assert "Next step hint:" in tool_messages[0].content
    assert "Write a design document" in tool_messages[0].content


@pytest.mark.asyncio
async def test_execute_tools_marks_failed_tool_result_as_error_status(tmp_path):
    graph = _graph(tmp_path)

    class FakeTools:
        async def execute_tool(self, tid, _targs, _ctx):
            assert tid == "read"
            return ToolResult(output="File not found: missing.py", metadata={"error": True})

    async def allow_all(tool_calls, **_kwargs):
        return tool_calls, []

    graph.tools = FakeTools()
    graph._authorize_tool_calls = allow_all

    parent = AIMessage(
        content="",
        tool_calls=[{
            "name": "read",
            "args": {"file_path": "missing.py"},
            "id": "call_read",
            "type": "tool_call",
        }],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
    })

    tool_message = result["messages"][0]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.tool_call_id == "call_read"
    assert tool_message.status == "error"


@pytest.mark.asyncio
async def test_execute_tools_escalates_and_blocks_repeated_tool_failure(tmp_path):
    graph = _graph(tmp_path)
    calls: list[dict] = []

    class FakeTools:
        async def execute_tool(self, tid, targs, _ctx):
            calls.append({"name": tid, "args": dict(targs)})
            return ToolResult(
                output=f"File not found: {targs['file_path']}",
                metadata={"error": True, "error_kind": "file_not_found"},
            )

    async def allow_all(tool_calls, **_kwargs):
        return tool_calls, []

    graph.tools = FakeTools()
    graph._authorize_tool_calls = allow_all

    async def run_read(call_id: str):
        parent = AIMessage(
            content="",
            tool_calls=[{
                "name": "read",
                "args": {"file_path": "missing.py"},
                "id": call_id,
                "type": "tool_call",
            }],
        )
        return await graph._execute_tools({
            "messages": [parent],
            "workspace": str(tmp_path),
            "persona": "voidx",
            "plan_mode": False,
        })

    await run_read("call_1")
    assert graph._pending_guidance == []

    await run_read("call_2")
    assert any("failed twice" in item.text for item in graph._pending_guidance)

    await run_read("call_3")
    assert any(
        "failed 3 times" in item.text and "Stop retrying it now" in item.text
        for item in graph._pending_guidance
    )

    result = await run_read("call_4")
    assert calls == [
        {"name": "read", "args": {"file_path": "missing.py"}},
        {"name": "read", "args": {"file_path": "missing.py"}},
        {"name": "read", "args": {"file_path": "missing.py"}},
    ]
    assert result["messages"][0].tool_call_id == "call_4"
    assert result["messages"][0].status == "error"
    assert "Runtime guard blocked repeated failed tool call" in result["messages"][0].content


@pytest.mark.asyncio
async def test_tool_execution_mixin_delegates_to_component():
    from voidx.agent.adapters.langgraph.execution import LangGraphExecution

    class FakeToolExecutor:
        def __init__(self):
            self.state = None
            self.tool_result_ok = None

        async def execute_tools(self, state, *, tool_result_ok=None):
            self.state = state
            self.tool_result_ok = tool_result_ok
            return {"messages": []}

    executor = FakeToolExecutor()

    def custom_result_ok(_result):
        return False

    host = SimpleNamespace(
        _tool_executor=executor,
        _tool_result_ok=custom_result_ok,
    )
    state = {"messages": []}

    result = await LangGraphExecution._execute_tools(host, state)

    assert result == {"messages": []}
    assert executor.state is state
    assert executor.tool_result_ok is custom_result_ok


@pytest.mark.asyncio
async def test_graph_authorization_blocks_lsp_format_in_plan_mode(tmp_path):
    graph = _graph(tmp_path)

    tool_call = {
        "name": "lsp_format",
        "args": {
            "file_path": "src/app.py",
            "start_line": 1,
            "start_character": 0,
            "end_line": 1,
            "end_character": 5,
        },
        "id": "call_1",
    }
    approved, denied = await graph._authorize_tool_calls(
        [tool_call],
        plan_mode=True,
        session_id="test",
    )

    assert approved == []
    assert len(denied) == 1
    assert denied[0][0]["id"] == "call_1"
    assert "not allowed" in denied[0][1]


@pytest.mark.asyncio
async def test_prepare_renders_plan_mode_constraint_without_mode_prompt(tmp_path):
    graph = _graph(tmp_path)

    async def empty_system(**_kwargs):
        return []

    async def empty_workflow_context(*_args, **_kwargs):
        return WorkflowRuntimeContext(instructions=[], active=[])

    graph._instruction.system = empty_system
    graph._instruction.workflow_context_for = empty_workflow_context

    messages = [HumanMessage(content="给个方案")]
    await graph._prepare_with_stream({
        "messages": messages,
        "workspace": str(tmp_path),
        "plan_mode": True,
        "persona": "voidx",
    })

    assert isinstance(messages[0], SystemMessage)
    assert "## Mode" not in messages[0].content
    assert "## PLAN MODE ACTIVE" not in messages[0].content
    assert "## Current Task State" in messages[-1].content
    assert "plan mode blocks write/insert/replace/edit" in messages[-1].content
    assert "implement delegation" not in messages[-1].content
