"""Regression tests for core graph behavior."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace


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


def _asked_tool_calls(batch):
    return [getattr(item, "tool_call", item) for item in batch]


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


def test_permission_decision_splits_readonly_and_implement_agents():
    service = PermissionService()

    assert service.decide("agent", "voidx") == "allow"
    assert service.decide("agent", "implement") == "ask"


@pytest.mark.asyncio
async def test_graph_authorization_auto_allows_readonly_agent(tmp_path):
    graph = _graph(tmp_path)
    approved, denied = await graph._authorize_tool_calls(
        [{
            "name": "agent",
            "args": {
                "agent": "voidx",
                "description": "Review current change",
                "goal_resolution": _child_goal_resolution("review", desc="Review current change", join="review", leave="review").model_dump(mode="json"),
                "result": _child_result_contract("review_result").model_dump(mode="json"),
            },
            "id": "call_1",
        }],
        runtime_persona="coordinate",
        plan_mode=False,
        session_id="test",
    )

    assert [tc["name"] for tc in approved] == ["agent"]
    assert denied == []


@pytest.mark.asyncio
async def test_graph_authorization_prompts_for_implement_agent(tmp_path):
    graph = _graph(tmp_path)
    asked: list[list[dict]] = []

    async def approve(tool_calls):
        asked.append(tool_calls)
        return "y"

    graph._ask_tool_permission = approve

    approved, denied = await graph._authorize_tool_calls(
        [{
            "name": "agent",
            "args": {
                "agent": "voidx",
                "description": "Implement feature",
                "goal_resolution": _child_goal_resolution("feature", desc="Implement feature", join="tdd", leave="verify").model_dump(mode="json"),
                "result": _child_result_contract().model_dump(mode="json"),
            },
            "id": "call_1",
        }],
        runtime_persona="coordinate",
        plan_mode=False,
        session_id="test",
    )

    assert [tc["name"] for tc in approved] == ["agent"]
    assert denied == []
    assert [[tc["args"]["goal_resolution"]["plan"]["join"] for tc in _asked_tool_calls(batch)] for batch in asked] == [["tdd"]]


@pytest.mark.asyncio
async def test_graph_authorization_respects_session_deny_for_safe_bash(tmp_path):
    graph = _graph(tmp_path)
    graph._permission.deny_silent("bash")

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "bash", "args": {"command": "ls"}, "id": "call_1"}],
        plan_mode=False,
        session_id="test",
    )

    assert approved == []
    assert len(denied) == 1
    assert "Permission denied" in denied[0][1]


@pytest.mark.asyncio
async def test_graph_authorization_never_approves_mixed_blocked_batch(tmp_path):
    graph = _graph(tmp_path)
    graph._permission.permission_mode = "safe"
    asked: list[list[object]] = []

    async def approve(tool_calls):
        asked.append(tool_calls)
        return "y"

    graph._ask_tool_permission = approve

    approved, denied = await graph._authorize_tool_calls(
        [
            {"name": "bash", "args": {"command": "rm -rf /"}, "id": "blocked"},
            {"name": "bash", "args": {"command": "echo $(date)"}, "id": "ask"},
        ],
        plan_mode=False,
        session_id="test",
    )

    assert [call["id"] for call in approved] == ["ask"]
    assert [call["id"] for call, _ in denied] == ["blocked"]
    assert len(asked) == 2
    assert [item.tool_call["id"] for item in asked[0]] == ["blocked"]
    assert [item.tool_call["id"] for item in asked[1]] == ["ask"]


@pytest.mark.asyncio
async def test_graph_authorization_asks_for_write_by_active_workflow_gate(tmp_path):
    graph = _graph(tmp_path)
    asked: list[list[dict]] = []

    async def approve(tool_calls):
        asked.append(tool_calls)
        return "y"

    graph._ask_tool_permission = approve

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "write", "args": {"file_path": "app.py", "content": "x"}, "id": "call_1"}],
        plan_mode=False,
        session_id="test",
        workflow_runs=[
            WorkflowRunState(name="brainstorm", status=WorkflowRunStatus.ACTIVE),
        ],
    )

    assert [[call["id"] for call in _asked_tool_calls(batch)] for batch in asked] == [["call_1"]]
    assert [call["id"] for call in approved] == ["call_1"]
    assert denied == []


@pytest.mark.asyncio
async def test_graph_authorization_uses_current_workflow_gate_only(tmp_path):
    graph = _graph(tmp_path)
    
    approved, denied = await graph._authorize_tool_calls(
        [{
            "name": "edit",
            "args": _edit_args("docs/specs/example-design-2026-06-13.md"),
            "id": "call_1",
        }],
        runtime_persona="implement",
        plan_mode=False,
        session_id="test",
        workflow_runs=[
            WorkflowRunState(name="design", status=WorkflowRunStatus.ACTIVE),
            WorkflowRunState(name="plan", status=WorkflowRunStatus.ACTIVE),
        ],
    )

    assert [call["id"] for call in approved] == ["call_1"]
    assert denied == []


@pytest.mark.asyncio
async def test_graph_authorization_allows_plan_gate_doc_paths_only(tmp_path):
    graph = _graph(tmp_path)
    asked: list[list[dict]] = []

    async def deny(tool_calls):
        asked.append(tool_calls)
        return "n"

    graph._ask_tool_permission = deny

    approved, denied = await graph._authorize_tool_calls(
        [
            {
                "name": "edit",
                "args": _edit_args("docs/specs/example-design-2026-06-13.md"),
                "id": "call_docs",
            },
            {
                "name": "edit",
                "args": _edit_args("src/app.py"),
                "id": "call_src",
            },
        ],
        runtime_persona="implement",
        plan_mode=False,
        session_id="test",
        workflow_runs=[
            WorkflowRunState(name="plan", status=WorkflowRunStatus.ACTIVE),
        ],
    )

    assert [call["id"] for call in approved] == ["call_docs"]
    assert [[call["id"] for call in _asked_tool_calls(batch)] for batch in asked] == [["call_src"]]
    assert [call["id"] for call, _reason in denied] == ["call_src"]
    assert denied[0][1] == "User denied: replace"


@pytest.mark.asyncio
async def test_graph_authorization_allowed_paths_match_nested_docs(tmp_path):
    graph = _graph(tmp_path)
    
    approved, denied = await graph._authorize_tool_calls(
        [{
            "name": "edit",
            "args": _edit_args("docs/specs/nested/example-design-2026-06-13.md"),
            "id": "call_nested_docs",
        }],
        runtime_persona="implement",
        plan_mode=False,
        session_id="test",
        workflow_runs=[
            WorkflowRunState(name="plan", status=WorkflowRunStatus.ACTIVE),
        ],
    )

    assert [call["id"] for call in approved] == ["call_nested_docs"]
    assert denied == []


@pytest.mark.asyncio
async def test_graph_authorization_does_not_block_tools_outside_active_workflow_node_allowlist(tmp_path):
    graph = _graph(tmp_path)
    
    approved, denied = await graph._authorize_tool_calls(
        [{
            "name": "todo",
            "args": {"todos": [{"content": "track work", "status": "active"}]},
            "id": "call_todo",
        }],
        runtime_persona="implement",
        plan_mode=False,
        session_id="test",
        workflow_runs=[
            WorkflowRunState(name="tdd", status=WorkflowRunStatus.ACTIVE),
        ],
    )

    assert [call["id"] for call in approved] == ["call_todo"]
    assert denied == []


@pytest.mark.asyncio
async def test_graph_authorization_asks_for_workflow_gate_tools_instead_of_denying(tmp_path):
    graph = _graph(tmp_path)
    asked: list[list[dict]] = []

    async def approve(tool_calls):
        asked.append(tool_calls)
        return "y"

    graph._ask_tool_permission = approve

    approved, denied = await graph._authorize_tool_calls(
        [{
            "name": "edit",
            "args": _edit_args("src/app.py"),
            "id": "call_src",
        }],
        runtime_persona="implement",
        plan_mode=False,
        session_id="test",
        workflow_runs=[
            WorkflowRunState(name="plan", status=WorkflowRunStatus.ACTIVE),
        ],
    )

    assert [[call["id"] for call in _asked_tool_calls(batch)] for batch in asked] == [["call_src"]]
    assert [call["id"] for call in approved] == ["call_src"]
    assert denied == []


@pytest.mark.asyncio
async def test_graph_authorization_asks_for_persona_blocked_write(tmp_path):
    graph = _graph(tmp_path)
    asked: list[list[dict]] = []

    async def approve(tool_calls):
        asked.append(tool_calls)
        return "y"

    graph._ask_tool_permission = approve

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "edit", "args": {"file_path": "test.py", "edits": []}, "id": "call_1"}],
        runtime_persona="coordinate",
        plan_mode=False,
        session_id="test",
    )

    assert [tc["name"] for tc in approved] == ["replace"]
    assert denied == []
    assert len(asked) == 1

@pytest.mark.asyncio
async def test_permission_result_uses_transient_output(tmp_path):
    graph = _graph(tmp_path)
    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()

    class FakeApp:
        def __init__(self):
            self.notices: list[str] = []

        async def ask_choice(self, _prompt, _choices, details=None):
            return "a"

        def set_notice(self, text: str) -> None:
            self.notices.append(text)

    app = FakeApp()
    graph._app = app
    try:
        approved, denied = await graph._authorize_tool_calls(
            [{"name": "write", "args": {"file_path": "app.py", "op": "append", "new_string": "x"}, "id": "call_1"}],
            runtime_persona="implement",
            plan_mode=False,
            session_id="test",
        )

        assert [tc["name"] for tc in approved] == ["write"]
        assert denied == []
        assert app.notices == []
        rendered = "\n".join(test_dock.tree.render(100))
        assert "tools allowed for this session" not in rendered
    finally:
        test_dock.deactivate()
        set_dock(None)


@pytest.mark.asyncio
async def test_permission_prompt_uses_dock_details_when_events_are_active(tmp_path):
    graph = _graph(tmp_path)
    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    received_details: list | None = None

    class FakeApp:
        async def ask_choice(self, _prompt, _choices, details=None):
            nonlocal received_details
            received_details = details
            return "y"

    graph._app = FakeApp()
    try:
        graph._ui.events.start(DockEventConsumer(test_dock))
        await graph._ui.events.request(TurnStarted(text="demo"))

        choice = await graph._ask_tool_permission([
            {
                "name": "bash",
                "args": {"command": "npm install lodash"},
                "id": "call_1",
            }
        ])
        await graph._ui.events.drain()

        assert choice == "y"
        assert received_details == [{
            "name": "bash",
            "pattern": "npm install lodash",
            "args": {"command": "npm install lodash"},
            "risk": None,
            "allowed_scopes": [],
            "default_scope": None,
        }]
        record = test_dock.status_record("permission:request")
        assert record is not None
        assert record.label == "Requesting"
        assert "bash" in record.detail
        assert "npm install lodash" in record.detail
    finally:
        await graph._ui.events.stop()
        test_dock.deactivate()
        set_dock(None)


@pytest.mark.asyncio
async def test_read_only_permission_prompt_limits_choices_to_once(tmp_path):
    graph = _graph(tmp_path)
    graph._permission.set_permission_mode("read_only")
    captured_choices = None

    class FakeApp:
        async def ask_choice(self, _prompt, choices, details=None):
            nonlocal captured_choices
            captured_choices = choices
            return "y"

    graph._app = FakeApp()

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "bash", "args": {"command": "cat input.txt > output.txt"}, "id": "call_1"}],
        plan_mode=False,
        session_id="s",
    )

    assert [tc["name"] for tc in approved] == ["bash"]
    assert denied == []
    assert captured_choices == [
        ("Yes", "y", "Allow this tool use once"),
        ("No", "n", "Deny these tools"),
    ]


@pytest.mark.asyncio
async def test_permission_prompt_details_include_risk_and_scopes(tmp_path):
    graph = _graph(tmp_path)
    graph._permission.set_permission_mode("safe")
    received_details = None

    class FakeApp:
        async def ask_choice(self, _prompt, _choices, details=None):
            nonlocal received_details
            received_details = details
            return "y"

    graph._app = FakeApp()

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "bash", "args": {"command": "npm install lodash"}, "id": "call_1"}],
        plan_mode=False,
        session_id="s",
    )

    assert [tc["name"] for tc in approved] == ["bash"]
    assert denied == []
    assert received_details == [{
        "name": "bash",
        "pattern": "npm install lodash",
        "args": {"command": "npm install lodash"},
        "risk": {
            "level": "extreme",
            "tags": ["dependency_install"],
            "reason": "shell policy deferred: dependency install command",
            "tool_name": "bash",
            "pattern": "npm install lodash",
        },
        "allowed_scopes": ["once"],
        "default_scope": "once",
    }]


@pytest.mark.asyncio
async def test_graph_authorization_attaches_approved_risk_token_to_approved_shell_call(tmp_path):
    graph = _graph(tmp_path)
    graph._permission.set_permission_mode("read_only")
    graph._permission.set_permission_mode("read_only")
    
    async def approve(_tool_calls):
        return "y"

    graph._ask_tool_permission = approve
    command = "cat input.txt > output.txt"

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "bash", "args": {"command": command}, "id": "call_1"}],
        plan_mode=False,
        session_id="s",
    )

    assert denied == []
    assert approved[0]["metadata"]["approved_risk"] == {
        "tool_name": "bash",
        "pattern": command,
        "risk_level": "extreme",
        "tags": ["dynamic_shell"],
        "reason": "shell policy deferred: compound shell syntax, compound shell operator",
    }


@pytest.mark.asyncio
async def test_blocked_permission_prompt_only_acknowledges_and_denies_execution(tmp_path):
    graph = _graph(tmp_path)
    graph._permission.set_permission_mode("safe")
    captured_choices = None
    captured_details = None

    class FakeApp:
        async def ask_choice(self, _prompt, choices, details=None):
            nonlocal captured_choices, captured_details
            captured_choices = choices
            captured_details = details
            return "n"

    graph._app = FakeApp()

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "bash", "args": {"command": "sudo true"}, "id": "call_blocked"}],
        plan_mode=False,
        session_id="s",
    )

    assert approved == []
    assert denied == [({"name": "bash", "args": {"command": "sudo true"}, "id": "call_blocked"}, "Blocked: sudo is blocked — privilege escalation")]
    assert captured_choices == [("Do not run", "n", "This command is blocked")]
    assert captured_details[0]["risk"]["level"] == "blocked"
    assert captured_details[0]["allowed_scopes"] == []
    assert captured_details[0]["default_scope"] is None


@pytest.mark.asyncio
async def test_blocked_permission_prompt_cannot_be_approved_with_yes(tmp_path):
    graph = _graph(tmp_path)
    graph._permission.set_permission_mode("safe")
    
    class FakeApp:
        async def ask_choice(self, _prompt, _choices, details=None):
            return "y"

    graph._app = FakeApp()

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "bash", "args": {"command": "sudo true"}, "id": "call_blocked"}],
        plan_mode=False,
        session_id="s",
    )

    assert approved == []
    assert denied == [({"name": "bash", "args": {"command": "sudo true"}, "id": "call_blocked"}, "Blocked: sudo is blocked — privilege escalation")]
