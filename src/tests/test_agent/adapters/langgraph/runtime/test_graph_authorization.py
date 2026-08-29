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
from voidx.presentation.output.dock import BottomInputDock, reset_dock, set_dock
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


def test_permission_decision_allows_all_agents():
    service = PermissionService()

    assert service.decide("agent", "voidx") == "allow"
    assert service.decide("agent", "implement") == "allow"


@pytest.mark.asyncio
async def test_graph_authorization_auto_allows_readonly_agent(tmp_path):
    graph = _graph(tmp_path)
    approved, denied = await graph._authorize_tool_calls(
        [{
            "name": "agent",
            "args": {
                "mode": "review",
                "goal": "Review current change",
                "detail": "Report concrete findings.",
                "scope": "src",
            },
            "id": "call_1",
        }],
        runtime_persona="coordinate",
        plan_mode=False,
        session_id="test",
    )

    assert [tc["name"] for tc in approved] == ["agent"]
    assert denied == []


@pytest.mark.parametrize("plan_mode", [False, True])
@pytest.mark.asyncio
async def test_graph_authorization_auto_allows_implement_agent(tmp_path, plan_mode):
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
                "mode": "implement",
                "goal": "Implement feature",
                "detail": "Implement and verify the feature.",
                "scope": "src",
            },
            "id": "call_1",
        }],
        runtime_persona="coordinate",
        plan_mode=plan_mode,
        session_id="test",
    )

    assert [tc["name"] for tc in approved] == ["agent"]
    assert denied == []
    assert asked == []


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
async def test_full_access_workflow_gate_advisory_allows_without_approval(tmp_path):
    graph = _graph(tmp_path)
    graph._permission.set_permission_mode("full_access")

    async def fail_if_asked(_tool_calls):
        pytest.fail("workflow gate should not prompt for approval under full_access")

    graph._ask_tool_permission = fail_if_asked

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "write", "args": {"file_path": "app.py", "content": "x"}, "id": "call_1"}],
        plan_mode=False,
        session_id="test",
        workflow_runs=[
            WorkflowRunState(name="brainstorm", status=WorkflowRunStatus.ACTIVE),
        ],
    )

    assert [call["id"] for call in approved] == ["call_1"]
    assert denied == []


@pytest.mark.asyncio
async def test_safe_mode_workflow_gate_still_prompts(tmp_path):
    """Under safe mode, workflow gate advisory does not bypass permission approval."""
    graph = _graph(tmp_path)
    graph._permission.set_permission_mode("safe")
    asked: list = []

    async def approve(tool_calls):
        asked.extend(tool_calls)
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

    assert [call["id"] for call in approved] == ["call_1"]
    assert denied == []
    assert len(asked) > 0


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
    asked: list[list[dict]] = []

    async def approve(tool_calls):
        asked.append(tool_calls)
        return "y"

    graph._ask_tool_permission = approve

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

    assert [[call["id"] for call in _asked_tool_calls(batch)] for batch in asked] == [["call_1"]]
    assert [call["id"] for call in approved] == ["call_1"]
    assert denied == []


@pytest.mark.asyncio
async def test_graph_authorization_plan_gate_no_longer_bypasses_doc_paths(tmp_path):
    """allowed_paths bypass removed: both docs and src edits go through normal permission."""
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

    assert approved == []
    assert [call["id"] for call, _reason in denied] == ["call_docs", "call_src"]
    assert all(reason == "User denied: replace" for _tc, reason in denied)


@pytest.mark.asyncio
async def test_graph_authorization_nested_docs_go_through_normal_permission(tmp_path):
    """allowed_paths bypass removed: nested docs edits go through normal permission."""
    graph = _graph(tmp_path)
    asked: list[list[dict]] = []

    async def approve(tool_calls):
        asked.append(tool_calls)
        return "y"

    graph._ask_tool_permission = approve

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

    assert [[call["id"] for call in _asked_tool_calls(batch)] for batch in asked] == [["call_nested_docs"]]
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
    dock_token = set_dock(test_dock)
    test_dock.begin_capture()

    class FakeApp:
        def __init__(self):
            self.notices: list[str] = []

        async def ask_choice(self, _prompt, _choices, details=None):
            return "a"

        def set_notice(self, text: str) -> None:
            self.notices.append(text)

    app = FakeApp()
    graph._ui.bind_frontend( app)
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
        reset_dock(dock_token)


@pytest.mark.asyncio
async def test_always_approval_for_shell_is_target_scoped(tmp_path):
    graph = _graph(tmp_path)
    asked: list[list[str]] = []

    async def approve(tool_calls):
        asked.append([decision.pattern for decision in tool_calls])
        return "a"

    graph._ask_tool_permission = approve

    first, denied = await graph._authorize_tool_calls(
        [{"name": "bash", "args": {"command": "./test.py --backend -- src/tests/test_permission/test_ai_approval.py"}, "id": "call_1"}],
        plan_mode=False,
        session_id="s",
    )
    second, denied_second = await graph._authorize_tool_calls(
        [{"name": "bash", "args": {"command": "./test.py --backend -- src/tests/test_agent/test_permission.py"}, "id": "call_2"}],
        plan_mode=False,
        session_id="s",
    )
    third, denied_third = await graph._authorize_tool_calls(
        [{"name": "bash", "args": {"command": "./other-test.py --backend"}, "id": "call_3"}],
        plan_mode=False,
        session_id="s",
    )

    assert [tc["id"] for tc in first] == ["call_1"]
    assert [tc["id"] for tc in second] == ["call_2"]
    assert [tc["id"] for tc in third] == ["call_3"]
    assert denied == []
    assert denied_second == []
    assert denied_third == []
    assert asked == [
        ["./test.py --backend -- src/tests/test_permission/test_ai_approval.py"],
        ["./other-test.py --backend"],
    ]



@pytest.mark.asyncio
async def test_ai_approval_reuses_successful_dangerous_call_without_review(tmp_path):
    from voidx.agent.adapters.langgraph.execution import _tool_call_key
    from voidx.tooling.application.ai_approval import AiApprovalResult
    from voidx.config import PermissionMode

    graph = _graph(tmp_path)
    graph._settings = Settings(str(tmp_path))
    graph._permission.permission_mode = PermissionMode.AI_APPROVAL.value
    reviewed: list[int] = []

    async def review(candidates, _settings):
        reviewed.append(len(candidates))
        return AiApprovalResult(
            allowed_ids=frozenset({candidates[0].tool_call["id"]}),
            reason="reviewed",
        )

    graph._ai_approval.review = review
    graph._notice_permission_result = lambda _message: None
    call = {"name": "git", "args": {"args": "push origin main"}, "id": "call_1"}

    first, first_denied = await graph._authorize_tool_calls([call], plan_mode=False, session_id="s")
    graph._record_successful_tool_call(first[0])
    second, second_denied = await graph._authorize_tool_calls(
        [{**call, "id": "call_2"}], plan_mode=False, session_id="s"
    )

    assert [item["id"] for item in first] == ["call_1"]
    assert [item["id"] for item in second] == ["call_2"]
    assert first_denied == second_denied == []
    assert reviewed == [1]
    assert _tool_call_key(call) == _tool_call_key({**call, "id": "different"})
    assert second[0]["metadata"]["approved_risk"]["approved_by"] == "cached"


@pytest.mark.asyncio
async def test_ai_approval_reviews_network_extreme_without_prompt(tmp_path):
    from voidx.config import PermissionMode
    from voidx.tooling.application.ai_approval import AiApprovalResult
    from voidx.tooling.domain.risk import RiskLevel, RiskTag

    graph = _graph(tmp_path)
    graph._settings = Settings(str(tmp_path))
    graph._permission.permission_mode = PermissionMode.AI_APPROVAL.value
    reviewed: list[tuple[str, RiskLevel, tuple[RiskTag, ...]]] = []

    async def review(candidates, _settings):
        reviewed.extend((item.pattern, item.risk.level, item.risk.tags) for item in candidates)
        return AiApprovalResult(
            allowed_ids=frozenset({candidates[0].tool_call["id"]}),
            reason="reviewed",
        )

    async def fail_if_asked(_tool_calls):
        pytest.fail("network command should be reviewed by AI before prompting")

    graph._ai_approval.review = review
    graph._ask_tool_permission = fail_if_asked
    graph._notice_permission_result = lambda _message: None

    call = {"name": "bash", "args": {"command": "curl https://example.com"}, "id": "call_1"}
    approved, denied = await graph._authorize_tool_calls([call], plan_mode=False, session_id="s")

    assert [item["id"] for item in approved] == ["call_1"]
    assert denied == []
    assert reviewed == [("curl https://example.com", RiskLevel.EXTREME, (RiskTag.NETWORK,))]
    assert approved[0]["metadata"]["approved_risk"]["approved_by"] == "ai"


@pytest.mark.asyncio
async def test_ai_approval_increments_counter_and_emits_refresh(tmp_path):
    from voidx.tooling.application.ai_approval import AiApprovalResult
    from voidx.config import PermissionMode
    from voidx.presentation.output.events import RefreshRequested

    graph = _graph(tmp_path)
    graph._settings = Settings(str(tmp_path))
    graph._permission.permission_mode = PermissionMode.AI_APPROVAL.value
    assert graph._permission.ai_approval_count == 0

    async def review(candidates, _settings):
        return AiApprovalResult(
            allowed_ids=frozenset({candidates[0].tool_call["id"]}),
            reason="reviewed",
        )

    graph._ai_approval.review = review
    graph._notice_permission_result = lambda _message: None

    emitted_events = []
    async def fake_emit(event):
        emitted_events.append(event)

    orig_emit = graph._ui.events.emit
    orig_via_events = graph._ui.via_events
    try:
        graph._ui.events.emit = fake_emit
        graph._ui.via_events = lambda: True

        call = {"name": "bash", "args": {"command": "curl https://example.com"}, "id": "call_1"}
        first, first_denied = await graph._authorize_tool_calls([call], plan_mode=False, session_id="s")

        assert [item["id"] for item in first] == ["call_1"]
        assert first_denied == []
        assert graph._permission.ai_approval_count == 1
        assert len(emitted_events) == 1
        assert isinstance(emitted_events[0], RefreshRequested)
    finally:
        graph._ui.events.emit = orig_emit
        graph._ui.via_events = orig_via_events



@pytest.mark.asyncio
async def test_ai_approval_notice_written_to_log_not_ui(tmp_path, monkeypatch):
    from voidx.config import PermissionMode
    from voidx.tooling.application.ai_approval import AiApprovalResult
    import voidx.agent.adapters.langgraph.runtime.permission_flow as perms_mod

    graph = _graph(tmp_path)
    graph._settings = Settings(str(tmp_path))
    graph._permission.permission_mode = PermissionMode.AI_APPROVAL.value

    async def review(candidates, _settings):
        return AiApprovalResult(
            allowed_ids=frozenset({candidates[0].tool_call["id"]}),
            reason="reviewed",
        )

    graph._ai_approval.review = review

    async def fail_if_asked(_tool_calls):
        pytest.fail("should not prompt")

    graph._ask_tool_permission = fail_if_asked

    printed: list[str] = []
    monkeypatch.setattr(graph._ui.ui, "print", lambda *a, **k: printed.append(str(a)))

    dock_calls: list[str] = []
    monkeypatch.setattr(
        graph._ui,
        "_dock",
        SimpleNamespace(
            active=False,
            append_message=lambda msg, **_kwargs: dock_calls.append(msg),
        ),
    )

    logged: list[dict] = []

    def fake_log(event, *, tool_name="", message="", session_id=None, **kwargs):
        logged.append({"event": event, "tool_name": tool_name, "message": message})

    monkeypatch.setattr(perms_mod, "log_tool_event", fake_log)

    call = {"name": "bash", "args": {"command": "curl https://example.com"}, "id": "call_1"}
    approved, denied = await graph._authorize_tool_calls([call], plan_mode=False, session_id="s")

    assert [item["id"] for item in approved] == ["call_1"]
    assert denied == []
    assert printed == []
    assert dock_calls == []
    assert len(logged) == 1
    assert logged[0]["event"] == "permission_notice"
    assert "AI 审批" in logged[0]["message"]
    assert "allow bash" in logged[0]["message"]




@pytest.mark.asyncio
async def test_successful_dangerous_call_cache_resets_with_runtime_state(tmp_path):
    graph = _graph(tmp_path)
    graph._successful_dangerous_calls.add("cached")
    graph._successful_dangerous_calls_session_id = "default"

    graph._reset_runtime_state_memory()

    assert graph._successful_dangerous_calls == set()
    assert graph._successful_dangerous_calls_session_id is None


@pytest.mark.asyncio
async def test_settings_update_clears_successful_dangerous_call_cache(tmp_path):
    graph = _graph(tmp_path)
    graph._successful_dangerous_calls.add("cached")
    graph._successful_dangerous_calls_session_id = "session"
    settings = Settings(str(tmp_path))

    await graph._apply_settings_update(settings)

    assert graph._successful_dangerous_calls == set()
    assert graph._successful_dangerous_calls_session_id is None




@pytest.mark.asyncio
async def test_clear_current_session_clears_successful_dangerous_call_cache(tmp_path):
    graph = _graph(tmp_path)
    graph._successful_dangerous_calls.add("cached")
    graph._successful_dangerous_calls_session_id = "default"

    await graph.clear_current_session()

    assert graph._successful_dangerous_calls == set()
    assert graph._successful_dangerous_calls_session_id is None

@pytest.mark.asyncio
async def test_always_approval_for_file_write_is_path_scoped(tmp_path):
    graph = _graph(tmp_path)
    asked: list[list[str]] = []

    async def approve(tool_calls):
        asked.append([decision.pattern for decision in tool_calls])
        return "a"

    graph._ask_tool_permission = approve

    first, denied = await graph._authorize_tool_calls(
        [{"name": "write", "args": {"file_path": "app.py", "new_string": "x"}, "id": "call_1"}],
        plan_mode=False,
        session_id="s",
    )
    second, denied_second = await graph._authorize_tool_calls(
        [{"name": "write", "args": {"file_path": "app.py", "new_string": "y"}, "id": "call_2"}],
        plan_mode=False,
        session_id="s",
    )
    third, denied_third = await graph._authorize_tool_calls(
        [{"name": "write", "args": {"file_path": "other.py", "new_string": "z"}, "id": "call_3"}],
        plan_mode=False,
        session_id="s",
    )

    assert [tc["id"] for tc in first] == ["call_1"]
    assert [tc["id"] for tc in second] == ["call_2"]
    assert [tc["id"] for tc in third] == ["call_3"]
    assert denied == []
    assert denied_second == []
    assert denied_third == []
    assert asked == [["app.py"], ["other.py"]]


@pytest.mark.asyncio
async def test_permission_prompt_uses_dock_details_when_events_are_active(tmp_path):
    graph = _graph(tmp_path)
    test_dock = BottomInputDock()
    dock_token = set_dock(test_dock)
    test_dock.begin_capture()
    received_details: list | None = None

    class FakeApp:
        async def ask_choice(self, _prompt, _choices, details=None):
            nonlocal received_details
            received_details = details
            return "y"

    graph._ui.bind_frontend( FakeApp())
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
        reset_dock(dock_token)


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

    graph._ui.bind_frontend( FakeApp())

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

    graph._ui.bind_frontend( FakeApp())

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


@pytest.mark.parametrize("permission_mode", ["project_trusted", "full_access"])
@pytest.mark.asyncio
async def test_auto_allowed_shell_call_carries_approved_risk(tmp_path, permission_mode: str):
    graph = _graph(tmp_path)
    graph._permission.set_permission_mode(permission_mode)

    async def fail_if_asked(_tool_calls):
        raise AssertionError(f"{permission_mode} should auto-allow workspace shell commands")

    graph._ask_tool_permission = fail_if_asked
    command = "./build.sh"

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "bash", "args": {"command": command}, "id": "call_1"}],
        plan_mode=False,
        session_id="s",
    )

    assert denied == []
    assert approved[0]["metadata"]["approved_risk"] == {
        "tool_name": "bash",
        "pattern": command,
        "risk_level": "dangerous",
        "tags": ["workspace_edit"],
        "reason": "unknown shell command",
    }


@pytest.mark.skipif(sys.platform == "win32", reason="bash is not registered on Windows")
@pytest.mark.parametrize("permission_mode", ["project_trusted", "full_access"])
@pytest.mark.asyncio
async def test_shell_script_executes_after_auto_allow(tmp_path, permission_mode: str):
    graph = _graph(tmp_path)
    graph._permission.set_permission_mode(permission_mode)
    script = tmp_path / "build.sh"
    script.write_text("#!/usr/bin/env bash\nprintf 'built\\n'\n", encoding="utf-8")
    script.chmod(0o755)

    async def fail_if_asked(_tool_calls):
        raise AssertionError(f"{permission_mode} should auto-allow workspace shell commands")

    graph._ask_tool_permission = fail_if_asked
    parent = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "bash",
                "args": {"command": "./build.sh"},
                "id": "call_build",
                "type": "tool_call",
            },
        ],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
    })

    tool_message = next(message for message in result["messages"] if isinstance(message, ToolMessage))
    assert tool_message.status == "success"
    payload = json.loads(tool_message.content)
    assert payload["ok"] is True
    assert payload["stdout"] == "built\n"


@pytest.mark.skipif(sys.platform == "win32", reason="bash is not registered on Windows")
@pytest.mark.parametrize("permission_mode", ["read_only", "safe"])
@pytest.mark.asyncio
async def test_shell_script_executes_after_prompt_approval(tmp_path, permission_mode: str):
    graph = _graph(tmp_path)
    graph._permission.set_permission_mode(permission_mode)
    script = tmp_path / "build.sh"
    script.write_text("#!/usr/bin/env bash\nprintf 'built\\n'\n", encoding="utf-8")
    script.chmod(0o755)

    asked = 0

    async def approve(_tool_calls):
        nonlocal asked
        asked += 1
        return "y"

    graph._ask_tool_permission = approve
    parent = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "bash",
                "args": {"command": "./build.sh"},
                "id": "call_build",
                "type": "tool_call",
            },
        ],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
    })

    tool_message = next(message for message in result["messages"] if isinstance(message, ToolMessage))
    assert asked == 1
    assert tool_message.status == "success"
    payload = json.loads(tool_message.content)
    assert payload["ok"] is True
    assert payload["stdout"] == "built\n"


@pytest.mark.skipif(sys.platform == "win32", reason="bash is not registered on Windows")
@pytest.mark.asyncio
async def test_ai_approval_executor_success_is_reused_without_review(tmp_path):
    from voidx.config import PermissionMode
    from voidx.tooling.application.ai_approval import AiApprovalResult

    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    target = external / "result.txt"
    graph = _graph(workspace)
    graph._settings = Settings(str(workspace))
    graph._permission.set_permission_mode(PermissionMode.AI_APPROVAL.value)
    reviewed: list[str] = []

    async def review(candidates, _settings):
        reviewed.append(candidates[0].tool_call["id"])
        return AiApprovalResult(
            allowed_ids=frozenset({candidates[0].tool_call["id"]}),
            reason="reviewed",
        )

    graph._ai_approval.review = review

    for call_id in ("call_first", "call_second"):
        result = await graph._execute_tools({
            "messages": [AIMessage(content="", tool_calls=[{
                "name": "write",
                "args": {"file_path": str(target), "op": "write", "new_string": "built\n"},
                "id": call_id,
                "type": "tool_call",
            }])],
            "workspace": str(workspace),
            "persona": "voidx",
            "plan_mode": False,
        })
        tool_message = next(message for message in result["messages"] if isinstance(message, ToolMessage))
        assert tool_message.status == "success"

    assert target.read_text(encoding="utf-8") == "built\n"
    assert reviewed == ["call_first"]


@pytest.mark.skipif(sys.platform == "win32", reason="bash is not registered on Windows")
@pytest.mark.asyncio
async def test_ai_approval_executor_failure_is_not_reused(tmp_path):
    from voidx.config import PermissionMode
    from voidx.tooling.application.ai_approval import AiApprovalResult

    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    target = external / "result.txt"
    graph = _graph(workspace)
    graph._settings = Settings(str(workspace))
    graph._permission.set_permission_mode(PermissionMode.AI_APPROVAL.value)
    reviewed: list[str] = []

    async def review(candidates, _settings):
        reviewed.append(candidates[0].tool_call["id"])
        return AiApprovalResult(
            allowed_ids=frozenset({candidates[0].tool_call["id"]}),
            reason="reviewed",
        )

    graph._ai_approval.review = review

    class FailingWriteTool:
        id = "write"
        description = "failing write"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args, ctx):
            return ToolResult(output="write failed", metadata={"error": True})

    graph.tools.replace("write", FailingWriteTool(), "failing write", {"type": "object", "properties": {}})

    for call_id in ("call_first", "call_second"):
        result = await graph._execute_tools({
            "messages": [AIMessage(content="", tool_calls=[{
                "name": "write",
                "args": {"file_path": str(target), "op": "write", "new_string": "built\n"},
                "id": call_id,
                "type": "tool_call",
            }])],
            "workspace": str(workspace),
            "persona": "voidx",
            "plan_mode": False,
        })
        tool_message = next(message for message in result["messages"] if isinstance(message, ToolMessage))
        assert tool_message.status == "error"

    assert reviewed == ["call_first", "call_second"]


@pytest.mark.asyncio
async def test_ai_approval_failure_reason_is_shown_in_permission_details(tmp_path):
    from voidx.config import PermissionMode
    from voidx.tooling.application.ai_approval import AiApprovalResult

    graph = _graph(tmp_path)
    graph._settings = Settings(str(tmp_path))
    graph._permission.set_permission_mode(PermissionMode.AI_APPROVAL.value)
    captured_details = None

    async def review(_candidates, _settings):
        return AiApprovalResult(reason="timeout")

    class FakeApp:
        async def ask_choice(self, _prompt, _choices, details=None):
            nonlocal captured_details
            captured_details = details
            return "n"

    graph._ai_approval.review = review
    graph._ui.bind_frontend( FakeApp())

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "bash", "args": {"command": "curl https://example.com"}, "id": "call_network"}],
        plan_mode=False,
        session_id="s",
    )

    assert approved == []
    assert len(denied) == 1
    assert captured_details is not None
    assert captured_details[0]["ai_approval_failure"] == (
        "AI approval failed: timed out; requesting human review."
    )


@pytest.mark.asyncio
async def test_ai_approval_direct_mcp_tool_is_always_allowed(tmp_path):
    from voidx.config import PermissionMode

    graph = _graph(tmp_path)
    graph._settings = Settings(str(tmp_path))
    graph._permission.set_permission_mode(PermissionMode.AI_APPROVAL.value)
    reviewed = False
    asked = False

    async def review(_candidates, _settings):
        nonlocal reviewed
        reviewed = True
        pytest.fail("direct MCP tools must not use AI approval")

    async def approve(_tool_calls):
        nonlocal asked
        asked = True
        pytest.fail("direct MCP tools must not prompt for approval")

    graph._ai_approval.review = review
    graph._ask_tool_permission = approve

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "mcp__remote", "args": {"operation": "read"}, "id": "call_remote"}],
        plan_mode=False,
        session_id="s",
    )

    assert [item["id"] for item in approved] == ["call_remote"]
    assert denied == []
    assert reviewed is False
    assert asked is False


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

    graph._ui.bind_frontend( FakeApp())

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

    graph._ui.bind_frontend( FakeApp())

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "bash", "args": {"command": "sudo true"}, "id": "call_blocked"}],
        plan_mode=False,
        session_id="s",
    )

    assert approved == []
    assert denied == [({"name": "bash", "args": {"command": "sudo true"}, "id": "call_blocked"}, "Blocked: sudo is blocked — privilege escalation")]


@pytest.mark.asyncio
async def test_clear_current_session_invalidates_cached_thread_states(tmp_path):
    session = await create_session(workspace=str(tmp_path))
    graph = _graph(tmp_path)
    graph._session = session
    try:
        from voidx.agent.adapters.langgraph.runtime.thread_context import _state_for_context

        state = await _state_for_context(graph, session.id, thread_id="web-thread")
        state.session_msg_cache = [HumanMessage(content="old conversation")]

        await graph.clear_current_session()

        rebound = await _state_for_context(graph, session.id, thread_id="web-thread")
        assert rebound is not state
        assert rebound.session_msg_cache is None
        assert await load_messages(session.id) == []
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_permission_prompt_is_cleared_when_frontend_wait_is_cancelled(tmp_path):
    graph = _graph(tmp_path)
    test_dock = BottomInputDock()
    dock_token = set_dock(test_dock)
    test_dock.begin_capture()

    class CancelledApp:
        async def ask_choice(self, _prompt, _choices, details=None):
            raise asyncio.CancelledError

    graph._ui.bind_frontend(CancelledApp())
    try:
        graph._ui.events.start(DockEventConsumer(test_dock))
        await graph._ui.events.request(TurnStarted(text="demo"))

        with pytest.raises(asyncio.CancelledError):
            await graph._authorize_tool_calls(
                [{"name": "bash", "args": {"command": "npm test"}, "id": "call_1"}],
                plan_mode=False,
                session_id="s",
            )

        await graph._ui.events.drain()
        assert test_dock.status_record("permission:request") is None
    finally:
        await graph._ui.events.stop()
        test_dock.deactivate()
        reset_dock(dock_token)


@pytest.mark.asyncio
async def test_clear_current_session_removes_persisted_runtime_state(tmp_path):
    from voidx.agent.adapters.persistence.runtime_state_repository import load_runtime_state

    session = await create_session(workspace=str(tmp_path))
    graph = _graph(tmp_path)
    graph._session = session
    graph._task_state.set_goal("old goal")
    graph._compaction_summary = "old summary"
    await graph.persist_runtime_state()
    try:
        await graph.clear_current_session()
        runtime = await load_runtime_state(session.id)
        assert runtime.task_state.current_goal is None
        assert runtime.compaction_summary == ""
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_clear_current_session_removes_workflow_todo_and_tool_results(tmp_path):
    from voidx.agent.adapters.persistence.runtime_state_repository import load_runtime_state
    from voidx.agent.adapters.tools.result_storage import persist_named_tool_result
    from voidx.agent.domain.task.todo import TodoRunItem, TodoRunState, TodoStatus
    from voidx.agent.domain.automation.workflow import WorkflowRoute

    session = await create_session(workspace=str(tmp_path))
    graph = _graph(tmp_path)
    graph._session = session
    graph._task_state.set_goal("old goal")
    graph._task_state.workflow_route = WorkflowRoute(route=["debug"])
    graph._task_state.todo_state = TodoRunState(
        summary="old todo",
        total=1,
        active=1,
        active_items=[TodoRunItem(id="old", content="old item", status=TodoStatus.ACTIVE)],
        items=[TodoRunItem(id="old", content="old item", status=TodoStatus.ACTIVE)],
    )
    graph._compaction_summary = "old summary"
    await graph.persist_runtime_state()
    result_path = persist_named_tool_result(
        "large output", "clear-test", session_id=session.id, workspace=str(tmp_path)
    )
    try:
        await graph.clear_current_session()
        runtime = await load_runtime_state(session.id)
        assert runtime.task_state.current_goal is None
        assert runtime.task_state.workflow_route is None
        assert runtime.task_state.workflow_runs == {}
        assert runtime.task_state.todo_state is None
        assert runtime.compaction_summary == ""
        assert not Path(result_path).exists()
    finally:
        await delete_session(session.id)
