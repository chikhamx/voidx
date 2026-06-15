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
    BASE_SYSTEM_PROMPT,
    VOIDX_PROMPT,
    get_agent,
    get_visible_agents,
    persona_prompt_for_llm,
)
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
from voidx.runtime import Goal, GoalType, PendingApproval, TaskIntent
from voidx.skills.context import SKILL_CONTEXT_MARKER, SKILL_TOOL_CONTEXT_MARKER, render_skill_context
from voidx.workflow.context import WORKFLOW_CONTEXT_MARKER
from voidx.workflow.policy import workflow_activations
from voidx.workflow.runtime import WorkflowRunState, WorkflowRunStatus
from voidx.agent.task_state import TaskState, ToolStatePatch
from voidx.tools.base import ToolContext, ToolResult
from voidx.tools.agent import AgentTool
from voidx.tools.registry import ToolRegistry
from voidx.ui.output.dock import BottomInputDock, set_dock
from voidx.ui.output.events import DockEventConsumer, TurnStarted, ui_events


def _graph(tmp_path):
    cfg = Config(workspace=str(tmp_path))
    return VoidXGraph(cfg, api_key=None)


def _task_state_json(**kwargs):
    return TaskState(**kwargs).model_dump(mode="json")


def _result_task_state(result: dict) -> TaskState:
    return TaskState.model_validate(result["task_state"])


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


def test_graph_registers_agent_tool_not_task_tool(tmp_path):
    graph = _graph(tmp_path)
    ids = graph.tools.ids()

    assert "agent" in ids
    assert "agent_parallel" not in ids
    assert "on_intent" not in ids
    assert "clarify" in ids
    assert "plan_checkpoint" in ids
    assert "load_skills" in ids
    assert "task" not in ids


def test_agent_parallel_tool_not_registered_when_disabled(tmp_path):
    graph = _graph(tmp_path)

    assert "agent_parallel" not in graph.tools.ids()


def test_parallel_subagents_disabled_prompt_hides_capability():
    agent = get_agent("voidx")
    assert agent is not None

    prompt = persona_prompt_for_llm(agent, parallel_subagents_enabled=False)

    assert "Delegate at most one child agent in a response" in prompt
    assert "multiple `agent` tool calls" not in prompt
    assert "run concurrently" not in prompt


def test_parallel_subagents_enabled_prompt_exposes_capability():
    agent = get_agent("voidx")
    assert agent is not None

    prompt = persona_prompt_for_llm(agent, parallel_subagents_enabled=True)

    assert "multiple `agent` tool calls" in prompt
    assert "run concurrently" in prompt
    assert "Delegate at most one child agent in a response" not in prompt


def test_agent_tool_description_hides_parallel_when_disabled(tmp_path):
    graph = _graph(tmp_path)
    agent_def = graph.tools.get_def("agent")

    assert agent_def is not None
    assert "run concurrently" not in agent_def.description
    assert "multiple `agent` tool calls" not in agent_def.description


def test_agent_tool_description_exposes_parallel_when_enabled(tmp_path):
    graph = VoidXGraph(
        Config(
            workspace=str(tmp_path),
            parallel_subagents=ParallelSubagentsConfig(enabled=True),
        ),
        api_key=None,
    )
    agent_def = graph.tools.get_def("agent")

    assert agent_def is not None
    assert "multiple `agent` tool calls" in agent_def.description
    assert "run concurrently" in agent_def.description


def test_orchestrator_prompt_mentions_delegation_gate():
    agent = get_agent("voidx")
    assert agent is not None

    prompt = persona_prompt_for_llm(agent, parallel_subagents_enabled=False)
    schema = AgentTool(runner=None).parameters_schema()

    assert "Do not delegate single-file reads" in prompt
    assert "simple searches" in prompt
    assert "straightforward tasks you can do directly" in prompt
    assert {
        "persona",
        "max_steps",
        "delegation_reason",
        "expected_output",
        "parent_evidence",
    }.issubset(set(schema["required"]))


def test_voidx_persona_prompt_declares_core_rules():
    assert "Runtime workflow gates take precedence over persona prompts" in VOIDX_PROMPT
    assert "Subagents do not interact with the user" in VOIDX_PROMPT
    assert "Switch persona" in VOIDX_PROMPT
    assert "implement persona" not in VOIDX_PROMPT


def test_base_system_prompt_registers_all_runtime_personas():
    for persona in ("coordinate", "explore", "plan", "implement", "review"):
        assert f"**{persona}**" in VOIDX_PROMPT


@pytest.mark.asyncio
async def test_clear_applies_saved_parallel_subagents_config(tmp_path):
    session = await create_session(
        workspace=str(tmp_path),
        provider="mimo",
        model="mimo-v2.5",
    )
    settings = Settings(str(tmp_path))
    settings.set_parallel_subagents(ParallelSubagentsConfig(enabled=True, max_concurrent=3))
    graph = VoidXGraph(
        Config(workspace=str(tmp_path)),
        api_key=None,
        session=session,
        settings=settings,
    )

    assert graph.config.parallel_subagents == ParallelSubagentsConfig()
    assert "multiple `agent` tool calls" not in graph.tools.get_def("agent").description

    await graph.clear_current_session()

    assert graph.config.parallel_subagents == ParallelSubagentsConfig(enabled=True, max_concurrent=3)
    agent_def = graph.tools.get_def("agent")
    assert agent_def is not None
    assert "multiple `agent` tool calls" in agent_def.description
    assert "run concurrently" in agent_def.description


@pytest.mark.asyncio
async def test_resume_applies_saved_parallel_subagents_config(tmp_path):
    session = await create_session(
        workspace=str(tmp_path),
        provider="mimo",
        model="mimo-v2.5",
    )
    settings = Settings(str(tmp_path))
    settings.set_parallel_subagents(ParallelSubagentsConfig(enabled=True, max_concurrent=3))
    graph = VoidXGraph(
        Config(workspace=str(tmp_path)),
        api_key=None,
        settings=settings,
    )

    assert graph.config.parallel_subagents == ParallelSubagentsConfig()
    assert "multiple `agent` tool calls" not in graph.tools.get_def("agent").description

    await graph.resume_session(session)

    assert graph.config.parallel_subagents == ParallelSubagentsConfig(enabled=True, max_concurrent=3)
    agent_def = graph.tools.get_def("agent")
    assert agent_def is not None
    assert "multiple `agent` tool calls" in agent_def.description
    assert "run concurrently" in agent_def.description


def test_graph_session_date_uses_session_creation_date(tmp_path):
    session = SessionInfo(
        id="s1",
        workspace=str(tmp_path),
        created_at="2026-06-06T12:00:00",
        updated_at="2026-06-07T12:00:00",
    )

    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None, session=session)

    assert graph._session_date.startswith("2026-06-06 ")


def test_orchestrator_has_direct_edit_tools():
    agent = get_agent("voidx")

    assert agent is not None
    assert {"write", "edit"}.issubset(set(agent.tools))
    assert {"clarify", "plan_checkpoint", "load_skills"}.issubset(set(agent.tools))
    assert get_agent("sub-voidx") is None
    assert get_agent("explore") is None
    assert get_agent("plan") is None
    assert get_agent("implement") is None
    assert get_agent("review") is None
    assert get_visible_agents() == [agent]
    assert "load_skills" in agent.tools
    assert agent.can_write is True


def test_tool_contract_labels_agent_identity_not_runtime_persona():
    agent = get_agent("voidx")

    assert agent is not None
    assert "- Agent identity: voidx" in agent.tool_contract
    assert "- Persona: voidx" not in agent.tool_contract


def test_persona_prompt_rejects_unregistered_agent_name():
    agent = AgentDef(
        name="orchesrator",
        description="typo",
        when_to_use="never",
        tools=[],
        can_write=False,
        can_delegate=False,
    )

    with pytest.raises(ValueError, match="No persona prompt registered"):
        _ = agent.persona_prompt


def test_brainstorm_workflow_does_not_write_design_doc():
    from voidx.workflow.dag import DEFAULT_WORKFLOW_DAG

    node = DEFAULT_WORKFLOW_DAG.nodes["brainstorm"]
    actions = [step.action for step in node.workflow]
    descriptions = [step.description for step in node.workflow]

    assert "Write design doc" not in actions
    assert not any("docs/specs" in description for description in descriptions)


def test_refactor_goal_starts_with_brainstorm_only():
    activations = workflow_activations(
        "Refactor workflow gates",
        task_intent="coding",
        goal_type="refactor",
    )

    assert [activation.name for activation in activations] == ["brainstorm"]


def test_plan_mode_does_not_pre_activate_plan_gate():
    activations = workflow_activations(
        "写 spec 文档",
        task_intent="coding",
        goal_type="design",
        interaction_mode=InteractionMode.PLAN.value,
    )

    assert [activation.name for activation in activations] == ["brainstorm"]


def test_plan_mode_explicit_plan_request_still_starts_with_brainstorm_only():
    activations = workflow_activations(
        "直接写实施计划",
        agent="plan",
        task_intent="coding",
        goal_type="design",
        interaction_mode=InteractionMode.PLAN.value,
    )

    assert [activation.name for activation in activations] == ["brainstorm"]


def test_internal_title_and_compaction_are_not_registered_agents():
    assert get_agent("compaction") is None
    assert get_agent("title") is None


def test_permission_decision_splits_readonly_and_implement_agents():
    service = PermissionService()

    assert service.decide("agent", "voidx") == "allow"
    assert service.decide("agent", "implement") == "ask"


@pytest.mark.asyncio
async def test_graph_authorization_auto_allows_readonly_agent(tmp_path):
    graph = _graph(tmp_path)
    graph._permission.approval_policy = "untrusted"
    approved, denied = await graph._authorize_tool_calls(
        [{"name": "agent", "args": {"agent": "voidx", "persona": "explore"}, "id": "call_1"}],
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
        [{"name": "agent", "args": {"agent": "voidx", "persona": "implement"}, "id": "call_1"}],
        runtime_persona="coordinate",
        plan_mode=False,
        session_id="test",
    )

    assert [tc["name"] for tc in approved] == ["agent"]
    assert denied == []
    assert [[tc["args"]["persona"] for tc in batch] for batch in asked] == [["implement"]]


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
async def test_graph_authorization_asks_for_write_by_active_workflow_gate(tmp_path):
    graph = _graph(tmp_path)
    graph._permission.approval_policy = "on-request"
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

    assert [[call["id"] for call in batch] for batch in asked] == [["call_1"]]
    assert [call["id"] for call in approved] == ["call_1"]
    assert denied == []


@pytest.mark.asyncio
async def test_graph_authorization_uses_current_workflow_gate_only(tmp_path):
    graph = _graph(tmp_path)
    graph._permission.approval_policy = "on-request"

    approved, denied = await graph._authorize_tool_calls(
        [{
            "name": "edit",
            "args": {
                "file_path": "docs/specs/example-design-2026-06-13.md",
                "edits": [{"old_string": "old", "new_string": "new"}],
            },
            "id": "call_1",
        }],
        runtime_persona="implement",
        plan_mode=False,
        session_id="test",
        workflow_runs=[
            WorkflowRunState(name="design-doc", status=WorkflowRunStatus.ACTIVE),
            WorkflowRunState(name="plan", status=WorkflowRunStatus.ACTIVE),
        ],
    )

    assert [call["id"] for call in approved] == ["call_1"]
    assert denied == []


@pytest.mark.asyncio
async def test_graph_authorization_allows_plan_gate_doc_paths_only(tmp_path):
    graph = _graph(tmp_path)
    graph._permission.approval_policy = "on-request"
    asked: list[list[dict]] = []

    async def deny(tool_calls):
        asked.append(tool_calls)
        return "n"

    graph._ask_tool_permission = deny

    approved, denied = await graph._authorize_tool_calls(
        [
            {
                "name": "edit",
                "args": {
                    "file_path": "docs/specs/example-design-2026-06-13.md",
                    "edits": [{"old_string": "old", "new_string": "new"}],
                },
                "id": "call_docs",
            },
            {
                "name": "edit",
                "args": {
                    "file_path": "src/app.py",
                    "edits": [{"old_string": "old", "new_string": "new"}],
                },
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
    assert [[call["id"] for call in batch] for batch in asked] == [["call_src"]]
    assert [call["id"] for call, _reason in denied] == ["call_src"]
    assert denied[0][1] == "User denied: edit"


@pytest.mark.asyncio
async def test_graph_authorization_allowed_paths_match_nested_docs(tmp_path):
    graph = _graph(tmp_path)
    graph._permission.approval_policy = "on-request"

    approved, denied = await graph._authorize_tool_calls(
        [{
            "name": "edit",
            "args": {
                "file_path": "docs/specs/nested/example-design-2026-06-13.md",
                "edits": [{"old_string": "old", "new_string": "new"}],
            },
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
    graph._permission.approval_policy = "on-request"

    approved, denied = await graph._authorize_tool_calls(
        [{
            "name": "todo",
            "args": {"todos": [{"content": "track work", "status": "in_progress"}]},
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
    graph._permission.approval_policy = "on-request"
    asked: list[list[dict]] = []

    async def approve(tool_calls):
        asked.append(tool_calls)
        return "y"

    graph._ask_tool_permission = approve

    approved, denied = await graph._authorize_tool_calls(
        [{
            "name": "edit",
            "args": {
                "file_path": "src/app.py",
                "edits": [{"old_string": "old", "new_string": "new"}],
            },
            "id": "call_src",
        }],
        runtime_persona="implement",
        plan_mode=False,
        session_id="test",
        workflow_runs=[
            WorkflowRunState(name="plan", status=WorkflowRunStatus.ACTIVE),
        ],
    )

    assert [[call["id"] for call in batch] for batch in asked] == [["call_src"]]
    assert [call["id"] for call in approved] == ["call_src"]
    assert denied == []


@pytest.mark.asyncio
async def test_graph_authorization_asks_for_persona_blocked_write(tmp_path):
    graph = _graph(tmp_path)
    graph._permission.approval_policy = "on-request"
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

    assert [tc["name"] for tc in approved] == ["edit"]
    assert denied == []
    assert [[tc["name"] for tc in batch] for batch in asked] == [["edit"]]


@pytest.mark.asyncio
async def test_permission_result_uses_transient_output(tmp_path):
    graph = _graph(tmp_path)
    graph._permission.approval_policy = "untrusted"
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
            [{"name": "write", "args": {"file_path": "app.py", "content": "x"}, "id": "call_1"}],
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
async def test_graph_on_request_auto_approves_need_ask_tools(tmp_path):
    graph = _graph(tmp_path)
    graph._permission.approval_policy = "on-request"

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "write", "args": {"file_path": "app.py", "content": "x"}, "id": "call_1"}],
        runtime_persona="implement",
        plan_mode=False,
        session_id="test",
    )

    assert [tc["name"] for tc in approved] == ["write"]
    assert denied == []


@pytest.mark.asyncio
async def test_graph_on_failure_still_asks_for_unsafe_bash(tmp_path):
    graph = _graph(tmp_path)
    graph._permission.approval_policy = "on-failure"

    async def deny(_tool_calls):
        return "n"

    graph._ask_tool_permission = deny
    approved, denied = await graph._authorize_tool_calls(
        [{"name": "bash", "args": {"command": "python -m pytest"}, "id": "call_1"}],
        plan_mode=False,
        session_id="test",
    )

    assert approved == []
    assert len(denied) == 1
    assert "User denied" in denied[0][1]


def test_tool_result_ok_detects_structured_failures():
    from voidx.agent.graph.tool_execution import GraphToolExecutionMixin

    assert GraphToolExecutionMixin._tool_result_ok(ToolResult(output="ok", metadata={"exit_code": 0}))
    assert not GraphToolExecutionMixin._tool_result_ok(ToolResult(output="failed", metadata={"exit_code": 2}))
    assert not GraphToolExecutionMixin._tool_result_ok(ToolResult(output="blocked", metadata={"blocked": True}))
    assert not GraphToolExecutionMixin._tool_result_ok(ToolResult(output="error", metadata={"error": True}))


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
    assert any("failed twice" in item for item in graph._pending_guidance)

    await run_read("call_3")
    assert any("failed 3 times" in item and "Stop retrying it now" in item for item in graph._pending_guidance)

    result = await run_read("call_4")
    assert calls == [
        {"name": "read", "args": {"file_path": "missing.py"}},
        {"name": "read", "args": {"file_path": "missing.py"}},
        {"name": "read", "args": {"file_path": "missing.py"}},
    ]
    assert result["messages"][0].tool_call_id == "call_4"
    assert "Runtime guard blocked repeated failed tool call" in result["messages"][0].content


@pytest.mark.asyncio
async def test_tool_execution_mixin_delegates_to_component():
    from voidx.agent.graph.tool_execution import GraphToolExecutionMixin

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

    result = await GraphToolExecutionMixin._execute_tools(host, state)

    assert result == {"messages": []}
    assert executor.state is state
    assert executor.tool_result_ok is custom_result_ok


@pytest.mark.asyncio
async def test_graph_authorization_blocks_lsp_format_in_plan_mode(tmp_path):
    graph = _graph(tmp_path)

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "lsp", "args": {"operation": "diagnostics", "file_path": "src/app.py"}, "id": "call_1"}],
        plan_mode=True,
        session_id="test",
    )

    assert approved == [{"name": "lsp", "args": {"operation": "diagnostics", "file_path": "src/app.py"}, "id": "call_1"}]
    assert denied == []


@pytest.mark.asyncio
async def test_prepare_injects_plan_mode_prompt(tmp_path):
    graph = _graph(tmp_path)

    async def empty_system():
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
    assert "## Mode" in messages[0].content
    assert "## PLAN MODE ACTIVE" in messages[0].content


def test_run_subagent_requires_explicit_max_steps():
    import inspect

    from voidx.agent.graph.subagent import run_subagent

    parameter = inspect.signature(run_subagent).parameters["max_steps"]
    assert parameter.default is inspect.Parameter.empty


@pytest.mark.asyncio
async def test_subagent_runner_passes_main_workflow_runtime_context(tmp_path, monkeypatch):
    import voidx.agent.graph.core as core_module

    graph = _graph(tmp_path)
    expected_context = WorkflowRuntimeContext(
        instructions=["instruction"],
        active=["tdd (implement persona)"],
        content="skill context",
        runs=[],
    )
    calls: list[dict] = []
    captured: dict[str, object] = {}

    async def fake_workflow_context_for(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return expected_context

    async def fake_run_subagent(*_args, **kwargs):
        captured.update(kwargs)
        return "child result"

    graph._instruction.workflow_context_for = fake_workflow_context_for
    monkeypatch.setattr(core_module, "_run_subagent", fake_run_subagent)

    result = await graph._subagent_runner(
        get_agent("voidx"),
        "Implement the feature",
        None,
        runtime_persona="implement",
        max_steps=8,
    )

    assert result == "child result"
    assert captured["max_steps"] == 8
    assert captured["workflow_runtime_context"] is expected_context
    assert "skill_selection" not in captured
    assert ("parent" + "_messages") not in captured
    assert calls[0]["kwargs"]["agent"] == "implement"
    assert calls[0]["kwargs"]["task_intent"] == "coding"
    assert calls[0]["kwargs"]["goal_type"] == "feature"
    assert calls[0]["kwargs"]["scope"] == "Implement the feature"


@pytest.mark.asyncio
async def test_subagent_runner_persists_lifecycle_jsonl(tmp_path, monkeypatch):
    import voidx.agent.graph.core as core_module

    graph = _graph(tmp_path)
    graph._session = await create_session(workspace=str(tmp_path))

    async def fake_workflow_context_for(*_args, **_kwargs):
        return WorkflowRuntimeContext(instructions=[], active=[], content="", runs=[])

    async def fake_run_subagent(*_args, **kwargs):
        kwargs["run_metadata"].update({
            "final_step": 2,
            "max_steps": 4,
            "finish_reason": "final_answer",
        })
        return "child result"

    graph._instruction.workflow_context_for = fake_workflow_context_for
    monkeypatch.setattr(core_module, "_run_subagent", fake_run_subagent)

    try:
        result = await graph._subagent_runner(
            get_agent("voidx"),
            "Inspect storage design",
            None,
            runtime_persona="explore",
            max_steps=4,
        )

        path = store.DATA_DIR / "sessions" / graph._session.id / "subagents" / "agent_0.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        assert result == "child result"
        assert [row["type"] for row in rows] == ["subagent_start", "subagent_finish"]
        assert rows[0]["agent_run_id"] == "agent_0"
        assert rows[0]["persona"] == "explore"
        assert rows[0]["description"] == "Inspect storage design"
        assert rows[1]["ok"] is True
        assert rows[1]["final_step"] == 2
        assert rows[1]["max_steps"] == 4
        assert rows[1]["finish_reason"] == "final_answer"
    finally:
        await delete_session(graph._session.id)


@pytest.mark.asyncio
async def test_subagent_runner_authorizes_with_child_interaction_mode(tmp_path, monkeypatch):
    import voidx.agent.graph.core as core_module

    graph = _graph(tmp_path)
    authorize_calls: list[dict] = []

    async def fake_workflow_context_for(*_args, **_kwargs):
        return WorkflowRuntimeContext(instructions=[], active=[], content="", runs=[])

    async def fake_authorize(tool_calls, **kwargs):
        authorize_calls.append(kwargs)
        return tool_calls, []

    async def fake_run_subagent(*_args, **kwargs):
        await kwargs["authorize_tools"]([])
        return "child result"

    graph._instruction.workflow_context_for = fake_workflow_context_for
    graph._authorize_tool_calls = fake_authorize
    monkeypatch.setattr(core_module, "_run_subagent", fake_run_subagent)

    result = await graph._subagent_runner(
        get_agent("voidx"),
        "Plan the feature",
        None,
        runtime_persona="plan",
        max_steps=6,
    )

    assert result == "child result"
    assert authorize_calls[0]["plan_mode"] is True
    assert authorize_calls[0]["interaction_mode"] == "plan"


@pytest.mark.asyncio
async def test_graph_authorization_does_not_treat_goal_as_read_only_mode(tmp_path):
    graph = _graph(tmp_path)
    graph._permission.approval_policy = "on-request"

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "edit", "args": {"file_path": "src/app.py"}, "id": "call_1"}],
        runtime_persona="implement",
        plan_mode=False,
        session_id="test",
        interaction_mode="goal",
    )

    assert [tc["name"] for tc in approved] == ["edit"]
    assert denied == []


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_graph_authorization_allows_read_only_bash(tmp_path):
    graph = _graph(tmp_path)

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "bash", "args": {"command": "ls"}, "id": "call_1"}],
        plan_mode=False,
        session_id="test",
        interaction_mode="auto",
    )

    assert [tc["name"] for tc in approved] == ["bash"]
    assert denied == []


@pytest.mark.asyncio
async def test_graph_authorization_prompts_for_edit(tmp_path):
    graph = _graph(tmp_path)
    asked: list[list[dict]] = []

    async def approve(tool_calls):
        asked.append(tool_calls)
        return "y"

    graph._ask_tool_permission = approve

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "edit", "args": {"file_path": "src/app.py"}, "id": "call_1"}],
        plan_mode=False,
        session_id="test",
        interaction_mode="auto",
    )

    assert [tc["name"] for tc in approved] == ["edit"]
    assert denied == []
    assert [[tc["name"] for tc in batch] for batch in asked] == [["edit"]]


@pytest.mark.asyncio
async def test_graph_authorization_respects_session_allow_for_edit(tmp_path):
    graph = _graph(tmp_path)
    graph._permission.allow_silent("edit")

    async def fail_if_asked(_tool_calls):
        pytest.fail("session-allowed edit should not prompt")

    graph._ask_tool_permission = fail_if_asked

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "edit", "args": {"file_path": "src/app.py"}, "id": "call_1"}],
        plan_mode=False,
        session_id="test",
        interaction_mode="auto",
    )

    assert [tc["name"] for tc in approved] == ["edit"]
    assert denied == []


@pytest.mark.asyncio
async def test_graph_authorization_prompts_for_unsafe_bash(tmp_path):
    graph = _graph(tmp_path)
    asked: list[list[dict]] = []

    async def approve(tool_calls):
        asked.append(tool_calls)
        return "y"

    graph._ask_tool_permission = approve

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "bash", "args": {"command": "python -m pytest"}, "id": "call_1"}],
        plan_mode=False,
        session_id="test",
        interaction_mode="auto",
    )

    assert [tc["name"] for tc in approved] == ["bash"]
    assert denied == []
    assert [[tc["name"] for tc in batch] for batch in asked] == [["bash"]]


@pytest.mark.asyncio
async def test_parallel_subagents_disabled_serializes_agent_calls(tmp_path):
    graph = _graph(tmp_path)
    active = 0
    max_active = 0

    class FakeAgentTool:
        id = "agent"
        description = "fake agent"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            nonlocal active, max_active
            call_id = current_parent_tool_call_id.get()
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return ToolResult(output=f"done {call_id}")

    graph.tools.register("agent", FakeAgentTool(), "fake agent", {"type": "object", "properties": {}})

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
        tool_calls=[
            {"name": "agent", "args": {"description": "a"}, "id": "call_a", "type": "tool_call"},
            {"name": "agent", "args": {"description": "b"}, "id": "call_b", "type": "tool_call"},
        ],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
    })

    messages = result["messages"]
    assert max_active == 1
    assert [msg.tool_call_id for msg in messages if isinstance(msg, ToolMessage)] == ["call_a", "call_b"]


@pytest.mark.asyncio
async def test_parallel_subagents_enabled_runs_agent_calls_concurrently(tmp_path):
    graph = VoidXGraph(
        Config(
            workspace=str(tmp_path),
            parallel_subagents=ParallelSubagentsConfig(enabled=True, max_concurrent=4),
        ),
        api_key=None,
    )
    active = 0
    max_active = 0

    class FakeAgentTool:
        id = "agent"
        description = "fake agent"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            nonlocal active, max_active
            call_id = current_parent_tool_call_id.get()
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return ToolResult(output=f"done {call_id}")

    graph.tools.register("agent", FakeAgentTool(), "fake agent", {"type": "object", "properties": {}})

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
        tool_calls=[
            {"name": "agent", "args": {"description": "a"}, "id": "call_a", "type": "tool_call"},
            {"name": "agent", "args": {"description": "b"}, "id": "call_b", "type": "tool_call"},
        ],
    )

    await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
    })

    assert max_active == 2


@pytest.mark.asyncio
async def test_parallel_subagents_preserves_tool_message_order(tmp_path):
    graph = VoidXGraph(
        Config(
            workspace=str(tmp_path),
            parallel_subagents=ParallelSubagentsConfig(enabled=True, max_concurrent=2),
        ),
        api_key=None,
    )

    class FakeAgentTool:
        id = "agent"
        description = "fake agent"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            call_id = current_parent_tool_call_id.get()
            if call_id == "call_a":
                await asyncio.sleep(0.02)
            return ToolResult(output=f"done {call_id}")

    graph.tools.register("agent", FakeAgentTool(), "fake agent", {"type": "object", "properties": {}})

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
        tool_calls=[
            {"name": "agent", "args": {"description": "a"}, "id": "call_a", "type": "tool_call"},
            {"name": "agent", "args": {"description": "b"}, "id": "call_b", "type": "tool_call"},
        ],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
    })

    tool_messages = [msg for msg in result["messages"] if isinstance(msg, ToolMessage)]
    assert [msg.tool_call_id for msg in tool_messages] == ["call_a", "call_b"]
    assert [msg.content for msg in tool_messages] == ["done call_a", "done call_b"]


@pytest.mark.asyncio
async def test_execute_tools_deduplicates_repeated_read_calls_in_same_segment(tmp_path):
    graph = _graph(tmp_path)
    calls: list[dict] = []

    class FakeTools:
        async def execute_tool(self, tid, targs, _ctx):
            calls.append({"name": tid, "args": dict(targs)})
            return ToolResult(output=f"read {len(calls)}")

    async def allow_all(tool_calls, **_kwargs):
        return tool_calls, []

    graph.tools = FakeTools()
    graph._authorize_tool_calls = allow_all
    parent = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "read",
                "args": {"file_path": "src/voidx/workflow/reconcile.py"},
                "id": "call_read_1",
                "type": "tool_call",
            },
            {
                "name": "read",
                "args": {"file_path": "src/voidx/workflow/reconcile.py"},
                "id": "call_read_2",
                "type": "tool_call",
            },
            {
                "name": "read",
                "args": {"file_path": "src/voidx/workflow/reconcile.py"},
                "id": "call_read_3",
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

    assert calls == [
        {"name": "read", "args": {"file_path": "src/voidx/workflow/reconcile.py"}}
    ]
    tool_messages = [msg for msg in result["messages"] if isinstance(msg, ToolMessage)]
    assert [msg.tool_call_id for msg in tool_messages] == [
        "call_read_1",
        "call_read_2",
        "call_read_3",
    ]
    assert "read 1" == tool_messages[0].content
    assert "Skipped duplicate read" in tool_messages[1].content
    assert "call_read_1" in tool_messages[1].content
    assert "Skipped duplicate read" in tool_messages[2].content
    assert "call_read_1" in tool_messages[2].content


@pytest.mark.asyncio
async def test_parallel_subagents_respects_max_concurrent(tmp_path):
    graph = VoidXGraph(
        Config(
            workspace=str(tmp_path),
            parallel_subagents=ParallelSubagentsConfig(enabled=True, max_concurrent=2),
        ),
        api_key=None,
    )
    active = 0
    max_active = 0

    class FakeAgentTool:
        id = "agent"
        description = "fake agent"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return ToolResult(output="done")

    graph.tools.register("agent", FakeAgentTool(), "fake agent", {"type": "object", "properties": {}})

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
        tool_calls=[
            {"name": "agent", "args": {"description": "a"}, "id": "call_a", "type": "tool_call"},
            {"name": "agent", "args": {"description": "b"}, "id": "call_b", "type": "tool_call"},
            {"name": "agent", "args": {"description": "c"}, "id": "call_c", "type": "tool_call"},
        ],
    )

    await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
    })

    assert max_active == 2


@pytest.mark.asyncio
async def test_parallel_subagents_failure_isolated(tmp_path):
    graph = VoidXGraph(
        Config(
            workspace=str(tmp_path),
            parallel_subagents=ParallelSubagentsConfig(enabled=True, max_concurrent=2),
        ),
        api_key=None,
    )

    class FakeAgentTool:
        id = "agent"
        description = "fake agent"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            call_id = current_parent_tool_call_id.get()
            if call_id == "call_a":
                raise RuntimeError("boom")
            return ToolResult(output=f"done {call_id}")

    graph.tools.register("agent", FakeAgentTool(), "fake agent", {"type": "object", "properties": {}})

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
        tool_calls=[
            {"name": "agent", "args": {"description": "a"}, "id": "call_a", "type": "tool_call"},
            {"name": "agent", "args": {"description": "b"}, "id": "call_b", "type": "tool_call"},
        ],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
    })

    messages = result["messages"]
    assert messages[0].tool_call_id == "call_a"
    assert "Tool execution error: boom" in messages[0].content
    assert messages[1].tool_call_id == "call_b"
    assert messages[1].content == "done call_b"


@pytest.mark.asyncio
async def test_parallel_subagents_continue_after_barrier_transaction(tmp_path):
    graph = VoidXGraph(
        Config(
            workspace=str(tmp_path),
            parallel_subagents=ParallelSubagentsConfig(enabled=True),
        ),
        api_key=None,
    )
    executed: list[str] = []

    class FakeBarrierTool:
        id = "plan_checkpoint"
        description = "fake barrier"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            executed.append("plan_checkpoint")
            return ToolResult(output="checkpoint ok")

    class FakeAgentTool:
        id = "agent"
        description = "fake agent"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            executed.append("agent")
            return ToolResult(output="agent ok")

    graph.tools.register("plan_checkpoint", FakeBarrierTool(), "fake barrier", {"type": "object", "properties": {}})
    graph.tools.register("agent", FakeAgentTool(), "fake agent", {"type": "object", "properties": {}})

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
        tool_calls=[
            {"name": "plan_checkpoint", "args": {}, "id": "call_plan", "type": "tool_call"},
            {"name": "agent", "args": {"description": "a"}, "id": "call_agent", "type": "tool_call"},
        ],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
    })

    assert executed == ["plan_checkpoint", "agent"]
    assert [msg.tool_call_id for msg in result["messages"]] == ["call_plan", "call_agent"]
    assert result["messages"][0].content == "checkpoint ok"
    assert result["messages"][1].content == "agent ok"


@pytest.mark.asyncio
async def test_parallel_subagents_plan_mode_blocks_implement(tmp_path):
    graph = VoidXGraph(
        Config(
            workspace=str(tmp_path),
            parallel_subagents=ParallelSubagentsConfig(enabled=True),
        ),
        api_key=None,
    )
    executed: list[str] = []

    class FakeAgentTool:
        id = "agent"
        description = "fake agent"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            executed.append(str(args.get("agent", "")))
            return ToolResult(output="should not run")

    graph.tools.register("agent", FakeAgentTool(), "fake agent", {"type": "object", "properties": {}})

    parent = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "agent",
                "args": {"agent": "implement", "description": "change auth"},
                "id": "call_a",
                "type": "tool_call",
            },
            {
                "name": "agent",
                "args": {"agent": "implement", "description": "change ui"},
                "id": "call_b",
                "type": "tool_call",
            },
        ],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": True,
        "interaction_mode": "plan",
    })

    assert executed == []
    assert [msg.tool_call_id for msg in result["messages"]] == ["call_a", "call_b"]
    assert all("BLOCKED by plan mode: cannot delegate to implement" in msg.content for msg in result["messages"])


@pytest.mark.asyncio
async def test_parallel_subagents_aggregate_ui_status_enabled_only(tmp_path):
    async def run_case(enabled: bool) -> list[object]:
        graph = VoidXGraph(
            Config(
                workspace=str(tmp_path),
                parallel_subagents=ParallelSubagentsConfig(enabled=enabled),
            ),
            api_key=None,
        )

        class FakeAgentTool:
            id = "agent"
            description = "fake agent"

            def parameters_schema(self):
                return {"type": "object", "properties": {}}

            async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
                await asyncio.sleep(0)
                return ToolResult(output="done")

        graph.tools.register("agent", FakeAgentTool(), "fake agent", {"type": "object", "properties": {}})

        async def allow_all(
            tool_calls,
            plan_mode: bool,
            session_id: str,
            interaction_mode=None,
        ):
            return tool_calls, []

        graph._authorize_tool_calls = allow_all
        events: list[object] = []

        class RecordingConsumer:
            def handle(self, event):
                events.append(event)
                return None

        parent = AIMessage(
            content="",
            tool_calls=[
                {"name": "agent", "args": {"description": "a"}, "id": "call_a", "type": "tool_call"},
                {"name": "agent", "args": {"description": "b"}, "id": "call_b", "type": "tool_call"},
            ],
        )

        test_dock = BottomInputDock()
        set_dock(test_dock)
        test_dock.begin_capture()
        ui_events.start(RecordingConsumer())
        try:
            await graph._execute_tools({
                "messages": [parent],
                "workspace": str(tmp_path),
                "persona": "voidx",
                "plan_mode": False,
            })
            await ui_events.drain()
        finally:
            await ui_events.stop()
            test_dock.deactivate()
            test_dock.reset()
            set_dock(None)
        return events

    disabled = await run_case(False)
    enabled = await run_case(True)

    disabled_labels = [getattr(event, "label", "") for event in disabled]
    enabled_labels = [getattr(event, "label", "") for event in enabled]

    assert "Running 2 child agents" not in disabled_labels
    assert "Finished 2 child agents" not in disabled_labels
    assert "Running 2 child agents" in enabled_labels
    assert "Finished 2 child agents" in enabled_labels


@pytest.mark.asyncio
async def test_execute_tools_does_not_inject_parallel_child_agent_buffers(tmp_path):
    graph = VoidXGraph(
        Config(
            workspace=str(tmp_path),
            parallel_subagents=ParallelSubagentsConfig(enabled=True),
        ),
        api_key=None,
    )

    class FakeAgentTool:
        id = "agent"
        description = "fake agent"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            call_id = current_parent_tool_call_id.get()
            if call_id == "call_a":
                await asyncio.sleep(0.01)
            return ToolResult(output=f"done {call_id}")

    graph.tools.register("agent", FakeAgentTool(), "fake agent", {"type": "object", "properties": {}})

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
        tool_calls=[
            {"name": "agent", "args": {"description": "a"}, "id": "call_a", "type": "tool_call"},
            {"name": "agent", "args": {"description": "b"}, "id": "call_b", "type": "tool_call"},
        ],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
    })

    messages = result["messages"]
    assert all(isinstance(msg, ToolMessage) for msg in messages)
    assert [msg.tool_call_id for msg in messages] == ["call_a", "call_b"]
    assert [msg.content for msg in messages] == ["done call_a", "done call_b"]


@pytest.mark.asyncio
async def test_execute_tools_emits_todo_updated_node(tmp_path):
    graph = _graph(tmp_path)

    class FakeTodoTool:
        id = "todo"
        description = "fake todo"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            return ToolResult(
                title="Todo",
                output="todo output",
                metadata={
                    "todo_summary": "0/1 done · 1 active · 0 pending",
                    "todo_items": [{"content": "wire event", "status": "in_progress"}],
                },
            )

    graph.tools.register("todo", FakeTodoTool(), "fake todo", {"type": "object", "properties": {}})

    async def allow_all(
        tool_calls,
        plan_mode: bool,
        session_id: str,
        interaction_mode=None,
    ):
        return tool_calls, []

    graph._authorize_tool_calls = allow_all
    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    ui_events.start(DockEventConsumer(test_dock))
    try:
        await ui_events.request(TurnStarted(text="demo"))
        parent = AIMessage(
            content="",
            tool_calls=[{
                "name": "todo",
                "args": {"todos": []},
                "id": "call_todo",
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

        todo_state = test_dock.todo_state()
        tool_nodes = [
            node
            for node in _tree_nodes(test_dock.tree.root)
            if node.node_type in {"tool_call", "tool_result"}
        ]

        assert todo_state is not None
        assert [(item.content, item.status) for item in todo_state.items] == [("wire event", "in_progress")]
        assert todo_state.summary == "0/1 done · 1 active · 0 pending"
        assert not any(node.node_type == "todo" for node in test_dock.tree.root.children)
        assert tool_nodes == []
        assert [message.tool_call_id for message in result["messages"]] == ["call_todo"]
        assert result["messages"][0].content == "todo output"
        assert result["todo_state"]["summary"] == "0/1 done · 1 active · 0 pending"
        assert result["todo_state"]["items"] == [{"content": "wire event", "status": "in_progress"}]
        assert graph._task_state.todo_state is not None
        assert graph._task_state.todo_state.items[0].content == "wire event"
    finally:
        await ui_events.stop()
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)


@pytest.mark.asyncio
async def test_execute_tools_warns_then_skips_repeated_todo_without_progress(tmp_path):
    graph = _graph(tmp_path)
    calls = 0

    class FakeTodoTool:
        id = "todo"
        description = "fake todo"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            nonlocal calls
            calls += 1
            return ToolResult(
                output="todo output",
                metadata={
                    "todo_summary": "0/1 done · 0 active · 1 pending",
                    "todo_items": [{"content": "same task", "status": "pending"}],
                },
            )

    async def allow_all(tool_calls, **_kwargs):
        return tool_calls, []

    graph.tools.register("todo", FakeTodoTool(), "fake todo", {"type": "object", "properties": {}})
    graph._authorize_tool_calls = allow_all

    async def run_todo(call_id: str):
        parent = AIMessage(
            content="",
            tool_calls=[{"name": "todo", "args": {"todos": []}, "id": call_id, "type": "tool_call"}],
        )
        return await graph._execute_tools({
            "messages": [parent],
            "workspace": str(tmp_path),
            "persona": "voidx",
            "plan_mode": False,
        })

    await run_todo("call_todo_1")
    await run_todo("call_todo_2")
    assert graph._pending_guidance == []

    await run_todo("call_todo_3")
    assert any("only called todo" in item for item in graph._pending_guidance)

    result = await run_todo("call_todo_4")
    assert calls == 3
    assert result["messages"][0].tool_call_id == "call_todo_4"
    assert "Runtime guard skipped repeated todo call" in result["messages"][0].content
    assert result.get("should_continue", True) is True


@pytest.mark.asyncio
async def test_execute_tools_no_progress_guidance_and_termination(tmp_path):
    graph = _graph(tmp_path)
    calls: list[str] = []

    class FakeTools:
        async def execute_tool(self, tid, _targs, _ctx):
            calls.append(tid)
            return ToolResult(output=f"{tid} ok")

    async def allow_all(tool_calls, **_kwargs):
        return tool_calls, []

    graph.tools = FakeTools()
    graph._authorize_tool_calls = allow_all

    async def run_tool(tool_name: str, call_id: str):
        parent = AIMessage(
            content="",
            tool_calls=[{"name": tool_name, "args": {}, "id": call_id, "type": "tool_call"}],
        )
        return await graph._execute_tools({
            "messages": [parent],
            "workspace": str(tmp_path),
            "persona": "voidx",
            "plan_mode": False,
        })

    await run_tool("plan_checkpoint", "call_1")
    await run_tool("advance_workflow", "call_2")
    assert graph._pending_guidance == []

    await run_tool("plan_checkpoint", "call_3")
    assert any("No meaningful progress" in item for item in graph._pending_guidance)

    await run_tool("advance_workflow", "call_4")
    result = await run_tool("plan_checkpoint", "call_5")

    assert calls == [
        "plan_checkpoint",
        "advance_workflow",
        "plan_checkpoint",
        "advance_workflow",
        "plan_checkpoint",
    ]
    assert result["should_continue"] is False
    assert any(
        isinstance(message, AIMessage) and "No meaningful progress" in str(message.content)
        for message in result["messages"]
    )


@pytest.mark.asyncio
async def test_execute_tools_wall_clock_guard_terminates_at_boundary(tmp_path):
    graph = _graph(tmp_path)
    graph._runtime_guards = RuntimeGuardState(
        wall_clock=WallClockGuardState(
            started_at=0.0,
            status_threshold_seconds=1.0,
            confirm_threshold_seconds=2.0,
        )
    )

    class FakeTools:
        async def execute_tool(self, tid, _targs, _ctx):
            assert tid == "read"
            return ToolResult(output="contents")

    async def allow_all(tool_calls, **_kwargs):
        return tool_calls, []

    graph.tools = FakeTools()
    graph._authorize_tool_calls = allow_all
    parent = AIMessage(
        content="",
        tool_calls=[{"name": "read", "args": {"file_path": "x.py"}, "id": "call_read", "type": "tool_call"}],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
    })

    assert result["should_continue"] is False
    assert any(
        isinstance(message, AIMessage) and "This turn has been running" in str(message.content)
        for message in result["messages"]
    )


@pytest.mark.asyncio
async def test_execute_tools_keeps_non_todo_result_in_mixed_batch(tmp_path):
    graph = _graph(tmp_path)

    class FakeTodoTool:
        id = "todo"
        description = "fake todo"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            return ToolResult(
                output="todo output",
                metadata={
                    "todo_summary": "0/1 done · 1 active · 0 pending",
                    "todo_items": [{"content": "track mixed batch", "status": "in_progress"}],
                },
            )

    class FakeReadTool:
        id = "read"
        description = "fake read"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            return ToolResult(output="read output")

    graph.tools.register("todo", FakeTodoTool(), "fake todo", {"type": "object", "properties": {}})
    graph.tools.register("read", FakeReadTool(), "fake read", {"type": "object", "properties": {}})

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
        tool_calls=[
            {"name": "todo", "args": {"todos": []}, "id": "call_todo", "type": "tool_call"},
            {"name": "read", "args": {}, "id": "call_read", "type": "tool_call"},
        ],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
    })

    assert [message.tool_call_id for message in result["messages"]] == ["call_todo", "call_read"]
    assert [message.content for message in result["messages"]] == ["todo output", "read output"]
    assert result["todo_state"]["items"] == [
        {"content": "track mixed batch", "status": "in_progress"}
    ]


@pytest.mark.asyncio
async def test_execute_tools_warns_on_malformed_todo_metadata_without_events(tmp_path, monkeypatch):
    graph = _graph(tmp_path)
    warnings: list[str] = []

    class FakeTodoTool:
        id = "todo"
        description = "fake todo"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            return ToolResult(output="todo output", metadata={"todo_items": "bad"})

    graph.tools.register("todo", FakeTodoTool(), "fake todo", {"type": "object", "properties": {}})
    monkeypatch.setattr(graph._ui.ui, "warn", warnings.append)

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
        tool_calls=[{"name": "todo", "args": {"todos": []}, "id": "call_todo", "type": "tool_call"}],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
    })

    assert [message.tool_call_id for message in result["messages"]] == ["call_todo"]
    assert result["messages"][0].content == "todo output"
    assert "todo_state" not in result
    assert warnings == ["Todo update ignored: tool returned malformed metadata."]


@pytest.mark.asyncio
async def test_subagent_full_output_reaches_orchestrator(tmp_path, monkeypatch):
    import voidx.agent.graph.subagent as subagent_module

    graph = _graph(tmp_path)
    child_output = "\n".join(f"child final line {index}" for index in range(1, 8))

    class FakeSubagentModel:
        def bind_tools(self, _tool_defs):
            return self

        async def astream(self, _messages):
            yield AIMessageChunk(content=[{"type": "thinking", "text": "child hidden thought"}])
            yield AIMessageChunk(content=child_output)

    monkeypatch.setattr(
        subagent_module,
        "create_chat_model",
        lambda *_args, **_kwargs: FakeSubagentModel(),
    )

    async def allow_all(
        tool_calls,
        plan_mode: bool,
        session_id: str,
        interaction_mode=None,
    ):
        return tool_calls, []

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
                "args": {
                    "agent": "voidx",
                    "persona": "explore",
                    "description": "inspect auth flow",
                    "max_steps": 5,
                    "delegation_reason": "user_requested",
                    "expected_output": "Return the full auth flow findings.",
                    "parent_evidence": "User requested delegated inspection.",
                },
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
        subagent = next(node for node in agent_tool.children if node.node_type == "subagent")
        child_streams = [
            node for node in subagent.children
            if node.node_type == "assistant" and "child final line" in node.header
        ]
        final_results = [node for node in agent_tool.children if node.node_type == "tool_result"]

        rendered = "\n".join(test_dock.tree.render(120))
        assert child_streams == []
        assert len(final_results) == 1
        final_result_text = "\n".join([final_results[0].header, *final_results[0].body_lines])
        assert "child final line 1" in final_result_text
        assert "child final line 5" in final_result_text
        assert "child final line 6" not in final_result_text
        assert "... (2 more lines omitted; full result passed to voidx)" in final_result_text
        assert "child hidden thought" not in rendered
        assert "child final line 7" not in rendered
        tool_messages = [message for message in result["messages"] if isinstance(message, ToolMessage)]
        assert tool_messages[0].tool_call_id == "call_agent"
        assert tool_messages[0].content == child_output
        assert not any(isinstance(message, AIMessage) and message.content == child_output for message in result["messages"])
    finally:
        await ui_events.stop()
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)


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
                "name": "compact_context",
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
                "name": "plan_checkpoint",
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
        "task_state": _task_state_json(
            current_intent=TaskIntent.CODING,
            pending_approval=PendingApproval(scope="Update runtime state handling"),
        ),
    })

    assert [message.tool_call_id for message in result["messages"]] == ["call_plan", "call_read"]
    task_state = _result_task_state(result)
    assert task_state.current_intent == TaskIntent.CODING
    assert task_state.current_goal is not None
    assert task_state.current_goal.target == "Update runtime state handling"
    assert task_state.current_goal.type == GoalType.FEATURE
    assert task_state.pending_approval is None
    assert result["messages"][1].content == "read after plan: coding:feature:Update runtime state handling"
    assert observed == {
        "task_intent": "coding",
        "goal_type": "feature",
        "goal_target": "Update runtime state handling",
    }


@pytest.mark.asyncio
async def test_barrier_failure_blocks_following_tools(tmp_path):
    graph = _graph(tmp_path)
    executed: list[str] = []

    class FailingBarrierTool:
        id = "plan_checkpoint"
        description = "fake failing barrier"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            executed.append("plan_checkpoint")
            return ToolResult(output="barrier failed", metadata={"error": True})

    class ExplodingReadTool:
        id = "read"
        description = "fake read"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            pytest.fail("read should be blocked after failed barrier")

    graph.tools.register("plan_checkpoint", FailingBarrierTool(), "fake failing barrier", {"type": "object", "properties": {}})
    graph.tools.register("read", ExplodingReadTool(), "fake read", {"type": "object", "properties": {}})

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
        tool_calls=[
            {"name": "plan_checkpoint", "args": {}, "id": "call_plan", "type": "tool_call"},
            {"name": "read", "args": {"file_path": "src/app.py"}, "id": "call_read", "type": "tool_call"},
        ],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
    })

    assert executed == ["plan_checkpoint"]
    assert [message.tool_call_id for message in result["messages"]] == ["call_plan", "call_read"]
    assert result["messages"][0].content == "barrier failed"
    assert result["messages"][1].content == "Blocked because a prior runtime barrier was failed."


@pytest.mark.asyncio
async def test_multiple_barriers_apply_patches_in_order(tmp_path):
    graph = _graph(tmp_path)
    observed: list[str] = []

    class FakeClarifyTool:
        id = "clarify"
        description = "fake clarify barrier"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            observed.append(f"clarify:{ctx.task_intent}:{ctx.goal_type}:{ctx.goal_target}")
            patch = ToolStatePatch(
                task_intent=TaskIntent.CODING,
                goal=Goal(type=GoalType.INSPECT, target="after intent"),
            )
            return ToolResult(
                output="clarify ok",
                metadata={"state_patch": patch.model_dump(mode="json", exclude_unset=True)},
            )

    class FakePlanTool:
        id = "plan_checkpoint"
        description = "fake plan barrier"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            observed.append(f"plan_checkpoint:{ctx.task_intent}:{ctx.goal_type}:{ctx.goal_target}")
            patch = ToolStatePatch(
                task_intent=TaskIntent.CODING,
                goal=Goal(type=GoalType.FEATURE, target="after plan", user_requested_write=True),
            )
            return ToolResult(
                output="plan ok",
                metadata={"state_patch": patch.model_dump(mode="json", exclude_unset=True)},
            )

    class RecordingReadTool:
        id = "read"
        description = "fake read"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            observed.append(f"read:{ctx.task_intent}:{ctx.goal_type}:{ctx.goal_target}")
            return ToolResult(output=f"read after barriers: {ctx.task_intent}:{ctx.goal_type}:{ctx.goal_target}")

    graph.tools.register("clarify", FakeClarifyTool(), "fake clarify", {"type": "object", "properties": {}})
    graph.tools.register("plan_checkpoint", FakePlanTool(), "fake plan", {"type": "object", "properties": {}})
    graph.tools.register("read", RecordingReadTool(), "fake read", {"type": "object", "properties": {}})

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
        tool_calls=[
            {"name": "clarify", "args": {}, "id": "call_clarify", "type": "tool_call"},
            {"name": "plan_checkpoint", "args": {}, "id": "call_plan", "type": "tool_call"},
            {"name": "read", "args": {"file_path": "src/app.py"}, "id": "call_read", "type": "tool_call"},
        ],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
        "interaction_mode": "auto",
        "task_state": _task_state_json(current_intent=TaskIntent.GENERAL),
    })

    assert [message.tool_call_id for message in result["messages"]] == [
        "call_clarify",
        "call_plan",
        "call_read",
    ]
    assert observed == [
        "clarify:general::",
        "plan_checkpoint:coding:inspect:after intent",
        "read:coding:feature:after plan",
    ]
    task_state = _result_task_state(result)
    assert task_state.current_intent == TaskIntent.CODING
    assert task_state.current_goal is not None
    assert task_state.current_goal.target == "after plan"
    assert result["messages"][2].content == "read after barriers: coding:feature:after plan"


@pytest.mark.asyncio
async def test_advance_workflow_transaction_reauthorizes_following_write(tmp_path):
    graph = _graph(tmp_path)
    graph._permission.approval_policy = "on-request"
    parent = AIMessage(
        content="",
        tool_calls=[
            {
                    "name": "advance_workflow",
                    "args": {
                        "workflow": "brainstorm",
                        "condition": "small_change",
                        "evidence": "stale design gate cleared",
                        "summary": "design gate cleared",
                },
                "id": "call_adv",
                "type": "tool_call",
            },
            {
                "name": "write",
                "args": {"file_path": "tmp-repro.txt", "content": "x"},
                "id": "call_write",
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
        "task_state": _task_state_json(
            current_intent=TaskIntent.CODING,
            workflow_runs={
                "brainstorm": WorkflowRunState(name="brainstorm", status=WorkflowRunStatus.ACTIVE),
            },
        ),
    })

    assert [message.tool_call_id for message in result["messages"]] == ["call_adv", "call_write"]
    assert "Blocked by workflow gate" not in result["messages"][1].content
    assert (tmp_path / "tmp-repro.txt").read_text() == "x"
    by_name = {run.name: run for run in _result_task_state(result).workflow_runs.values()}
    assert by_name["brainstorm"].status == WorkflowRunStatus.SATISFIED


@pytest.mark.asyncio
async def test_advance_workflow_done_stops_before_followup_llm_when_workflow_complete(tmp_path):
    graph = _graph(tmp_path)
    parent = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "advance_workflow",
                "args": {
                    "workflow": "verify",
                    "condition": "done",
                    "evidence": "focused verification passed",
                    "summary": "verification complete",
                },
                "id": "call_adv",
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
        "task_state": _task_state_json(
            current_intent=TaskIntent.CODING,
            workflow_runs={
                "verify": WorkflowRunState(name="verify", status=WorkflowRunStatus.ACTIVE),
            },
        ),
    })

    assert result["should_continue"] is False
    by_name = {run.name: run for run in _result_task_state(result).workflow_runs.values()}
    assert by_name["verify"].status == WorkflowRunStatus.SATISFIED


@pytest.mark.asyncio
async def test_advance_workflow_route_end_satisfies_without_successor(tmp_path):
    graph = _graph(tmp_path)
    parent = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "advance_workflow",
                "args": {
                    "workflow": "review",
                    "condition": "review_has_issues",
                    "evidence": "review verdict failed with actionable issues",
                    "summary": "review completed",
                },
                "id": "call_adv",
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
        "task_state": _task_state_json(
            current_intent=TaskIntent.CODING,
            workflow_route={"start": "review", "end": "review"},
            workflow_runs={
                "review": WorkflowRunState(name="review", status=WorkflowRunStatus.ACTIVE),
            },
        ),
    })

    assert "review_has_issues" in result["messages"][0].content
    assert result["should_continue"] is False
    by_name = {run.name: run for run in _result_task_state(result).workflow_runs.values()}
    assert by_name["review"].status == WorkflowRunStatus.SATISFIED
    assert "feedback" not in by_name


@pytest.mark.asyncio
async def test_advance_workflow_route_end_satisfies_non_review_without_successor(tmp_path):
    graph = _graph(tmp_path)
    parent = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "advance_workflow",
                "args": {
                    "workflow": "tdd",
                    "condition": "implemented",
                    "evidence": "implementation complete with focused test coverage",
                    "summary": "implementation complete",
                },
                "id": "call_adv",
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
        "task_state": _task_state_json(
            current_intent=TaskIntent.CODING,
            workflow_route={"start": "tdd", "end": "tdd"},
            workflow_runs={
                "tdd": WorkflowRunState(name="tdd", status=WorkflowRunStatus.ACTIVE),
            },
        ),
    })

    assert "implemented" in result["messages"][0].content
    assert result["should_continue"] is False
    by_name = {run.name: run for run in _result_task_state(result).workflow_runs.values()}
    assert by_name["tdd"].status == WorkflowRunStatus.SATISFIED
    assert "verify" not in by_name


@pytest.mark.asyncio
async def test_multiple_advance_workflow_done_calls_finish_batch_before_stopping(tmp_path):
    graph = _graph(tmp_path)
    parent = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "advance_workflow",
                "args": {
                    "workflow": "design-doc",
                    "condition": "done",
                    "evidence": "design doc archived",
                    "summary": "design doc complete",
                },
                "id": "call_design_done",
                "type": "tool_call",
            },
            {
                "name": "advance_workflow",
                "args": {
                    "workflow": "verify",
                    "condition": "done",
                    "evidence": "archive file exists and source file is removed",
                    "summary": "archive verification complete",
                },
                "id": "call_verify_done",
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
        "task_state": _task_state_json(
            current_intent=TaskIntent.CODING,
            workflow_runs={
                "design-doc": WorkflowRunState(name="design-doc", status=WorkflowRunStatus.ACTIVE),
                "verify": WorkflowRunState(name="verify", status=WorkflowRunStatus.ACTIVE),
            },
        ),
    })

    assert [message.tool_call_id for message in result["messages"]] == [
        "call_design_done",
        "call_verify_done",
    ]
    assert result["should_continue"] is False
    by_name = {run.name: run for run in _result_task_state(result).workflow_runs.values()}
    assert by_name["design-doc"].status == WorkflowRunStatus.SATISFIED
    assert by_name["verify"].status == WorkflowRunStatus.SATISFIED


@pytest.mark.asyncio
async def test_advance_workflow_non_terminal_transition_keeps_followup_llm_enabled(tmp_path):
    graph = _graph(tmp_path)
    parent = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "advance_workflow",
                "args": {
                    "workflow": "tdd",
                    "condition": "implemented",
                    "evidence": "red-green cycle completed",
                    "summary": "implementation complete",
                },
                "id": "call_adv",
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
        "task_state": _task_state_json(
            current_intent=TaskIntent.CODING,
            workflow_runs={
                "tdd": WorkflowRunState(name="tdd", status=WorkflowRunStatus.ACTIVE),
            },
        ),
    })

    assert result.get("should_continue", True) is True
    by_name = {run.name: run for run in _result_task_state(result).workflow_runs.values()}
    assert by_name["tdd"].status == WorkflowRunStatus.SATISFIED
    assert by_name["verify"].status == WorkflowRunStatus.ACTIVE


@pytest.mark.asyncio
async def test_auto_review_has_issues_stops_before_feedback_followup(tmp_path):
    graph = _graph(tmp_path)

    class FakeTools:
        async def execute_tool(self, tid, _targs, _ctx):
            assert tid == "agent"
            return ToolResult(
                output="verdict: FAIL\n\n## Issues\n- reviewer found a bug",
                metadata={"agent": "review"},
            )

    async def allow_all(tool_calls, **_kwargs):
        return tool_calls, []

    graph.tools = FakeTools()
    graph._authorize_tool_calls = allow_all
    parent = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "agent",
                "args": {"agent": "review", "description": "review recent changes"},
                "id": "call_review",
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
        "task_state": _task_state_json(
            current_intent=TaskIntent.CODING,
            workflow_runs={
                "review": WorkflowRunState(name="review", status=WorkflowRunStatus.ACTIVE),
            },
        ),
    })

    assert result["should_continue"] is False
    by_name = {run.name: run for run in _result_task_state(result).workflow_runs.values()}
    assert by_name["review"].status == WorkflowRunStatus.SATISFIED
    assert by_name["feedback"].status == WorkflowRunStatus.ACTIVE


@pytest.mark.asyncio
async def test_auto_review_has_issues_review_only_route_stops_without_feedback(tmp_path):
    graph = _graph(tmp_path)

    class FakeTools:
        async def execute_tool(self, tid, _targs, _ctx):
            assert tid == "agent"
            return ToolResult(
                output="verdict: FAIL\n\n## Issues\n- reviewer found a bug",
                metadata={"agent": "review"},
            )

    async def allow_all(tool_calls, **_kwargs):
        return tool_calls, []

    graph.tools = FakeTools()
    graph._authorize_tool_calls = allow_all
    parent = AIMessage(
        content="",
        tool_calls=[{
            "name": "agent",
            "args": {"agent": "review", "description": "review recent changes"},
            "id": "call_review",
            "type": "tool_call",
        }],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
        "interaction_mode": "auto",
        "task_state": _task_state_json(
            current_intent=TaskIntent.CODING,
            workflow_route={"start": "review", "end": "review"},
            workflow_runs={
                "review": WorkflowRunState(name="review", status=WorkflowRunStatus.ACTIVE),
            },
        ),
    })

    assert result["should_continue"] is False
    by_name = {run.name: run for run in _result_task_state(result).workflow_runs.values()}
    assert by_name["review"].status == WorkflowRunStatus.SATISFIED
    assert "feedback" not in by_name


@pytest.mark.asyncio
async def test_auto_review_has_issues_review_and_fix_route_continues_to_feedback(tmp_path):
    graph = _graph(tmp_path)

    class FakeTools:
        async def execute_tool(self, tid, _targs, _ctx):
            assert tid == "agent"
            return ToolResult(
                output="verdict: FAIL\n\n## Issues\n- reviewer found a bug",
                metadata={"agent": "review"},
            )

    async def allow_all(tool_calls, **_kwargs):
        return tool_calls, []

    graph.tools = FakeTools()
    graph._authorize_tool_calls = allow_all
    parent = AIMessage(
        content="",
        tool_calls=[{
            "name": "agent",
            "args": {"agent": "review", "description": "review recent changes"},
            "id": "call_review",
            "type": "tool_call",
        }],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
        "interaction_mode": "auto",
        "task_state": _task_state_json(
            current_intent=TaskIntent.CODING,
            workflow_route={"start": "review", "end": "verify"},
            workflow_runs={
                "review": WorkflowRunState(name="review", status=WorkflowRunStatus.ACTIVE),
            },
        ),
    })

    assert result.get("should_continue", True) is True
    by_name = {run.name: run for run in _result_task_state(result).workflow_runs.values()}
    assert by_name["review"].status == WorkflowRunStatus.SATISFIED
    assert by_name["feedback"].status == WorkflowRunStatus.ACTIVE


def test_execute_tools_router_honors_should_continue_false():
    from voidx.agent.graph.topology import route_after_execute_tools

    assert route_after_execute_tools({"should_continue": False}) == "end"
    assert route_after_execute_tools({"should_continue": True}) == "call_llm"
    assert route_after_execute_tools({}) == "call_llm"


@pytest.mark.asyncio
async def test_session_persistence_saves_only_new_ai_and_tool_messages(tmp_path):
    session = await create_session(workspace=str(tmp_path))
    try:
        await save_message(MessageRow(session_id=session.id, role="user", content="old question"))
        await save_message(MessageRow(session_id=session.id, role="assistant", content="old answer"))

        graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None, session=session)

        class FakeGraph:
            async def ainvoke(self, initial, _config):
                return {"messages": list(initial["messages"]) + [AIMessage(content="new answer")]}

        graph.graph = FakeGraph()

        test_dock = BottomInputDock()
        set_dock(test_dock)
        test_dock.begin_capture()
        try:
            await graph._run_once("new question")
        finally:
            test_dock.deactivate()
            test_dock.reset()
            set_dock(None)

        rows = await load_messages(session.id)
        assistant_contents = [row.content for row in rows if row.role == "assistant"]
        assert assistant_contents.count("old answer") == 1
        assert assistant_contents.count("new answer") == 1
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_skill_context_overlay_not_persisted_to_user_history(tmp_path):
    session = await create_session(workspace=str(tmp_path))
    try:
        graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None, session=session)

        class FakeGraph:
            async def ainvoke(self, initial, _config):
                return {
                    "messages": [
                        *initial["messages"],
                        HumanMessage(content=f"{SKILL_CONTEXT_MARKER}\n\n## Skill: docs\nBody-Hash: abc\n\nDocs body"),
                        AIMessage(content="new answer"),
                    ]
                }

        graph.graph = FakeGraph()

        test_dock = BottomInputDock()
        set_dock(test_dock)
        test_dock.begin_capture()
        try:
            await graph._run_once("new question")
        finally:
            test_dock.deactivate()
            test_dock.reset()
            set_dock(None)

        rows = await load_messages(session.id)
        assert [row.content for row in rows if row.role == "user"] == ["new question"]
        assert all(SKILL_CONTEXT_MARKER not in row.content for row in rows)
        assert all("Docs body" not in row.content for row in rows)
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_run_synthetic_turn_uses_display_text_without_losing_prompt(tmp_path):
    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None)
    captured: dict[str, list] = {}

    class FakeGraph:
        async def ainvoke(self, initial, _config):
            captured["messages"] = list(initial["messages"])
            return {"messages": list(initial["messages"]) + [AIMessage(content="ok")]}

    graph.graph = FakeGraph()

    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    try:
        await graph.run_synthetic_turn(
            "full initialization prompt with unique model marker",
            display_text="/init",
        )
        turn_header = test_dock.tree.root.children[0].header
        rendered = "\n".join(test_dock.tree.render(120))
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)

    assert turn_header == "[bold white]❯[/] /init"
    assert "full initialization prompt" not in rendered
    assert any(
        isinstance(message, HumanMessage)
        and message.content == "full initialization prompt with unique model marker"
        for message in captured["messages"]
    )


@pytest.mark.asyncio
async def test_run_once_wraps_explicit_skill_refs_in_user_message(tmp_path):
    skill_dir = tmp_path / ".voidx" / "skills" / "docs"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: docs\ndescription: Write docs\n---\nDocs body",
        encoding="utf-8",
    )
    session = await create_session(workspace=str(tmp_path))
    try:
        graph = VoidXGraph(
            Config(workspace=str(tmp_path)),
            api_key=None,
            session=session,
            settings=Settings(str(tmp_path)),
        )
        captured: dict[str, list] = {}

        class FakeGraph:
            async def ainvoke(self, initial, _config):
                captured["messages"] = list(initial["messages"])
                return {"messages": list(initial["messages"]) + [AIMessage(content="ok")]}

        graph.graph = FakeGraph()

        test_dock = BottomInputDock()
        set_dock(test_dock)
        test_dock.begin_capture()
        try:
            await graph._run_once("Use $docs for this README")
            turn_header = test_dock.tree.root.children[0].header
        finally:
            test_dock.deactivate()
            test_dock.reset()
            set_dock(None)

        user_message = captured["messages"][-1]
        assert isinstance(user_message, HumanMessage)
        assert user_message.content.startswith("用户指定了技能：\n- docs: Write docs")
        assert "Use for this README" in user_message.content
        assert "$docs" not in user_message.content
        assert "Docs body" not in user_message.content
        assert turn_header == "[bold white]❯[/] Use $docs for this README"

        rows = await load_messages(session.id)
        user_rows = [row for row in rows if row.role == "user"]
        assert user_rows[-1].content == user_message.content
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_run_once_persists_image_attachment_as_structured_user_message(tmp_path):
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    session = await create_session(workspace=str(tmp_path))
    try:
        graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None, session=session)

        class FakeGraph:
            async def ainvoke(self, initial, _config):
                user = initial["messages"][-1]
                assert isinstance(user.content, list)
                assert user.content[1]["type"] == "image_url"
                return {"messages": list(initial["messages"]) + [AIMessage(content="ok")]}

        graph.graph = FakeGraph()

        test_dock = BottomInputDock()
        set_dock(test_dock)
        test_dock.begin_capture()
        try:
            await graph._run_once("describe @shot.png")
        finally:
            test_dock.deactivate()
            test_dock.reset()
            set_dock(None)

        rows = await load_messages(session.id)
        user_rows = [row for row in rows if row.role == "user"]
        assert user_rows[-1].content_format == "structured"
        assert "image_url" in user_rows[-1].content
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_run_once_does_not_persist_compiled_overlay_to_user_history(tmp_path):
    session = await create_session(workspace=str(tmp_path))
    try:
        graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None, session=session)

        test_dock = BottomInputDock()
        set_dock(test_dock)
        test_dock.begin_capture()
        try:
            await graph._run_once("hello world")
        finally:
            test_dock.deactivate()
            test_dock.reset()
            set_dock(None)

        rows = await load_messages(session.id)
        user_rows = [row for row in rows if row.role == "user"]

        assert user_rows
        assert user_rows[-1].content == "hello world"
        assert "VOIDX_RUNTIME_CONTEXT" not in user_rows[-1].content
        assert "Runtime State" not in user_rows[-1].content
        assert "Active Skills" not in user_rows[-1].content
    finally:
        await delete_session(session.id)


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
                                "name": "plan_checkpoint",
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
            "plan_checkpoint",
        ]
        assert [row.tool_call_id for row in tool_rows] == ["call_clarify", "call_plan"]
        assert [row.content for row in tool_rows] == ['{"answer": "frontend"}', '{"decision": "approved"}']
    finally:
        await delete_session(session.id)


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


@pytest.mark.asyncio
async def test_prepare_does_not_auto_inject_project_skill_body(tmp_path):
    skill_dir = tmp_path / ".voidx" / "skills" / "docs"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: docs\ndescription: Documentation helper\n---\nWrite concise docs.",
        encoding="utf-8",
    )
    graph = VoidXGraph(
        Config(workspace=str(tmp_path)),
        api_key=None,
        settings=Settings(str(tmp_path)),
    )
    messages = [HumanMessage(content="Use $docs for this README")]
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
    assert isinstance(messages[1], HumanMessage)
    assert messages[1].content.startswith(WORKFLOW_CONTEXT_MARKER)
    assert "Skill: docs" not in messages[1].content
    assert "Write concise docs." not in messages[1].content


@pytest.mark.asyncio
async def test_prepare_injects_workflow_nodes_from_task_state(tmp_path):
    graph = VoidXGraph(
        Config(workspace=str(tmp_path)),
        api_key=None,
        settings=Settings(str(tmp_path)),
    )
    messages = [HumanMessage(content="对，可以")]
    state = {
        "messages": messages,
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
        "interaction_mode": "auto",
        "task_intent": "coding",
        "task_state": TaskState(
            current_intent=TaskIntent.CODING,
            current_goal=Goal(type=GoalType.BUGFIX, target="修复 runtime bug"),
        ).model_dump(mode="json"),
        "tool_results": {},
        "step_count": 0,
        "max_steps": 50,
        "should_continue": True,
    }

    result = await graph._prepare_with_stream(state)

    assert isinstance(messages[1], HumanMessage)
    assert messages[1].content.startswith(WORKFLOW_CONTEXT_MARKER)
    assert "Workflow Node: debug" in messages[1].content
    assert "Workflow Node: tdd" in messages[1].content
    assert "Workflow Node: verify" in messages[1].content
    task_context_message = next(
        message
        for message in messages
        if isinstance(message, HumanMessage) and "Active workflow nodes: debug" in str(message.content)
    )
    result_task_state = TaskState.model_validate(result["task_state"])
    assert [name for name in (result_task_state.workflow_runs or {})] == [
        "debug",
        "tdd",
        "verify",
    ]
    assert "Workflow run state: debug=active" in task_context_message.content


@pytest.mark.asyncio
async def test_implement_subagent_injects_workflow_nodes(tmp_path, monkeypatch):
    from voidx.agent.agents import get_agent
    import voidx.agent.graph.subagent as subagent_module

    captured: dict[str, list] = {}

    class FakeModel:
        def bind_tools(self, _tool_defs):
            return self

    async def fake_stream_llm(_model, messages, _renderer, _protocol):
        captured["messages"] = messages
        return AIMessage(content="done")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)
    workflow_context = await InstructionService(str(tmp_path)).workflow_context_for(
        "Implement the feature",
        agent="implement",
        task_intent="coding",
        goal_type="feature",
        interaction_mode=InteractionMode.AUTO.value,
        scope="Implement the feature",
    )

    output = await subagent_module.run_subagent(
        get_agent("voidx"),
        "Implement the feature",
        None,
        "test-key",
        Config(
            workspace=str(tmp_path),
            user_profile=UserProfile(language="zh-CN", tone="direct"),
        ),
        runtime_persona="implement",
        max_steps=4,
        workflow_runtime_context=workflow_context,
        debug=False,
    )

    assert output == "done"
    system_prompt = next(
        message.content
        for message in captured["messages"]
        if isinstance(message, SystemMessage)
    )
    assert "## Agent Role\n## Coordination" in system_prompt
    assert "## Runtime Constraints" in system_prompt
    assert "Do not interact with the user directly." in system_prompt
    assert "Do not start another child agent." in system_prompt
    workflow_context = next(
        message.content
        for message in captured["messages"]
        if isinstance(message, HumanMessage) and str(message.content).startswith(WORKFLOW_CONTEXT_MARKER)
    )
    rendered_user = next(
        message.content
        for message in captured["messages"]
        if isinstance(message, HumanMessage) and "Runtime State" in str(message.content)
    )
    assert "Workflow Node: brainstorm" in workflow_context
    assert "Workflow Node: tdd" in workflow_context
    assert "Workflow Node: verify" in workflow_context
    assert "Active workflow nodes: brainstorm" in rendered_user
    assert "User language: Chinese (Simplified) [zh-CN]" in rendered_user
    assert "Tone instruction: Be direct and practical. Lead with the answer or action." in rendered_user


@pytest.mark.asyncio
async def test_run_subagent_persists_assistant_messages_to_subagent_jsonl(tmp_path, monkeypatch):
    import voidx.agent.graph.subagent as subagent_module

    session = await create_session(workspace=str(tmp_path))

    class FakeModel:
        def bind_tools(self, _tool_defs):
            return self

    async def fake_stream_llm(_model, _messages, _renderer, _protocol):
        return AIMessage(content="child answer")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    try:
        output = await subagent_module.run_subagent(
            get_agent("voidx"),
            "Inspect child path",
            None,
            "test-key",
            Config(workspace=str(tmp_path)),
            runtime_persona="explore",
            max_steps=4,
            session_id=session.id,
            agent_id=3,
            debug=False,
        )

        path = store.DATA_DIR / "sessions" / session.id / "subagents" / "agent_3.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        assert output == "child answer"
        assert rows[-1]["type"] == "assistant_message"
        assert rows[-1]["agent_run_id"] == "agent_3"
        assert rows[-1]["step"] == 1
        assert rows[-1]["content_preview"] == "child answer"
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_run_subagent_persists_tool_results_to_subagent_jsonl(tmp_path, monkeypatch):
    import voidx.agent.graph.subagent as subagent_module

    session = await create_session(workspace=str(tmp_path))
    stream_calls: list[list] = []

    class FakeModel:
        def bind_tools(self, _tool_defs):
            return self

    class FakeToolRegistry:
        def filtered_copy(self, _allowed_ids):
            return self

        def tools_for_llm(self):
            return [{"name": "read", "description": "read", "input_schema": {}}]

        async def execute_tool(self, tid, _targs, _ctx):
            assert tid == "read"
            assert _ctx.session_id == session.id
            return ToolResult(output="file contents")

    async def fake_stream_llm(_model, messages, _renderer, _protocol):
        stream_calls.append(list(messages))
        if len(stream_calls) == 1:
            return AIMessage(
                content="",
                tool_calls=[{"name": "read", "args": {"file_path": "x.py"}, "id": "call_read", "type": "tool_call"}],
            )
        return AIMessage(content="done")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)
    monkeypatch.setattr(subagent_module, "ToolRegistry", FakeToolRegistry)

    try:
        output = await subagent_module.run_subagent(
            AgentDef(
                name="explore",
                description="test",
                when_to_use="test",
                tools=["read"],
                can_write=False,
                can_delegate=False,
            ),
            "Inspect child path",
            None,
            "test-key",
            Config(workspace=str(tmp_path)),
            runtime_persona="explore",
            max_steps=4,
            session_id=session.id,
            agent_id=5,
            debug=False,
        )

        path = store.DATA_DIR / "sessions" / session.id / "subagents" / "agent_5.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        assert output == "done"
        assert any(row["type"] == "tool_result" and row["tool_call_id"] == "call_read" for row in rows)
        tool_row = next(row for row in rows if row["type"] == "tool_result")
        assert tool_row["tool_name"] == "read"
        assert tool_row["content"] == "file contents"
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_run_subagent_injects_failure_loop_guidance(tmp_path, monkeypatch):
    import voidx.agent.graph.subagent as subagent_module

    stream_calls: list[list] = []
    tool_calls = 0

    class FakeModel:
        def bind_tools(self, _tool_defs):
            return self

    class FakeToolRegistry:
        def filtered_copy(self, _allowed_ids):
            return self

        def tools_for_llm(self):
            return [{"name": "read", "description": "read", "input_schema": {}}]

        async def execute_tool(self, tid, targs, _ctx):
            nonlocal tool_calls
            tool_calls += 1
            assert tid == "read"
            return ToolResult(
                output=f"File not found: {targs['file_path']}",
                metadata={"error": True, "error_kind": "file_not_found"},
            )

    async def fake_stream_llm(_model, messages, _renderer, _protocol):
        stream_calls.append(list(messages))
        if len(stream_calls) <= 2:
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": "read",
                    "args": {"file_path": "missing.py"},
                    "id": f"call_read_{len(stream_calls)}",
                    "type": "tool_call",
                }],
            )
        assert any("failed twice" in str(message.content) for message in messages)
        return AIMessage(content="done")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)
    monkeypatch.setattr(subagent_module, "ToolRegistry", FakeToolRegistry)

    output = await subagent_module.run_subagent(
        AgentDef(
            name="explore",
            description="test",
            when_to_use="test",
            tools=["read"],
            can_write=False,
            can_delegate=False,
        ),
        "Inspect child path",
        None,
        "test-key",
        Config(workspace=str(tmp_path)),
        runtime_persona="explore",
        max_steps=5,
        debug=False,
    )

    assert output == "done"
    assert tool_calls == 2


@pytest.mark.asyncio
async def test_run_subagent_terminates_after_no_progress_cycles(tmp_path, monkeypatch):
    import voidx.agent.graph.subagent as subagent_module

    stream_calls: list[list] = []
    executed_tools: list[str] = []

    class FakeModel:
        def bind_tools(self, _tool_defs):
            return self

    class FakeToolRegistry:
        def filtered_copy(self, _allowed_ids):
            return self

        def tools_for_llm(self):
            return [
                {"name": "plan_checkpoint", "description": "checkpoint", "input_schema": {}},
                {"name": "advance_workflow", "description": "advance", "input_schema": {}},
            ]

        async def execute_tool(self, tid, _targs, _ctx):
            executed_tools.append(tid)
            return ToolResult(output=f"{tid} ok")

    async def fake_stream_llm(_model, messages, _renderer, _protocol):
        stream_calls.append(list(messages))
        if len(stream_calls) == 4:
            assert any("No meaningful progress" in str(message.content) for message in messages)
        if len(stream_calls) <= 5:
            tool_name = "plan_checkpoint" if len(stream_calls) % 2 else "advance_workflow"
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": tool_name,
                    "args": {},
                    "id": f"call_{len(stream_calls)}",
                    "type": "tool_call",
                }],
            )
        return AIMessage(content="missed guard termination")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)
    monkeypatch.setattr(subagent_module, "ToolRegistry", FakeToolRegistry)

    output = await subagent_module.run_subagent(
        AgentDef(
            name="explore",
            description="test",
            when_to_use="test",
            tools=["plan_checkpoint", "advance_workflow"],
            can_write=False,
            can_delegate=False,
        ),
        "Inspect child path",
        None,
        "test-key",
        Config(workspace=str(tmp_path)),
        runtime_persona="explore",
        max_steps=8,
        debug=False,
    )

    assert "No meaningful progress" in output
    assert executed_tools == [
        "plan_checkpoint",
        "advance_workflow",
        "plan_checkpoint",
        "advance_workflow",
        "plan_checkpoint",
    ]


@pytest.mark.asyncio
async def test_run_subagent_wall_clock_guard_terminates_at_boundary(tmp_path, monkeypatch):
    import voidx.agent.graph.subagent as subagent_module

    executed_tools: list[str] = []

    class FakeModel:
        def bind_tools(self, _tool_defs):
            return self

    class FakeToolRegistry:
        def filtered_copy(self, _allowed_ids):
            return self

        def tools_for_llm(self):
            return [{"name": "plan_checkpoint", "description": "checkpoint", "input_schema": {}}]

        async def execute_tool(self, tid, _targs, _ctx):
            executed_tools.append(tid)
            return ToolResult(output=f"{tid} ok")

    async def fake_stream_llm(_model, _messages, _renderer, _protocol):
        if not executed_tools:
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": "plan_checkpoint",
                    "args": {},
                    "id": "call_plan",
                    "type": "tool_call",
                }],
            )
        return AIMessage(content="missed guard termination")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)
    monkeypatch.setattr(subagent_module, "ToolRegistry", FakeToolRegistry)
    monkeypatch.setattr(
        subagent_module.WallClockGuardState,
        "for_subagent",
        classmethod(lambda cls: WallClockGuardState(
            started_at=0.0,
            status_threshold_seconds=1.0,
            confirm_threshold_seconds=2.0,
        )),
    )

    output = await subagent_module.run_subagent(
        AgentDef(
            name="explore",
            description="test",
            when_to_use="test",
            tools=["plan_checkpoint"],
            can_write=False,
            can_delegate=False,
        ),
        "Inspect child path",
        None,
        "test-key",
        Config(workspace=str(tmp_path)),
        runtime_persona="explore",
        max_steps=4,
        debug=False,
    )

    assert executed_tools == ["plan_checkpoint"]
    assert "This turn has been running" in output


@pytest.mark.asyncio
async def test_run_subagent_repetitive_guard_runs_before_authorization(tmp_path, monkeypatch):
    import voidx.agent.graph.subagent as subagent_module

    stream_calls: list[list] = []
    authorized_batches: list[list[str]] = []
    executed_tools: list[str] = []

    class FakeModel:
        def bind_tools(self, _tool_defs):
            return self

    class FakeToolRegistry:
        def filtered_copy(self, _allowed_ids):
            return self

        def tools_for_llm(self):
            return [{"name": "todo", "description": "todo", "input_schema": {}}]

        async def execute_tool(self, tid, _targs, _ctx):
            executed_tools.append(tid)
            return ToolResult(output="todo output")

    async def fake_stream_llm(_model, messages, _renderer, _protocol):
        stream_calls.append(list(messages))
        return AIMessage(
            content="",
            tool_calls=[{
                "name": "todo",
                "args": {"todos": []},
                "id": f"call_todo_{len(stream_calls)}",
                "type": "tool_call",
            }],
        )

    async def authorize(tool_calls):
        authorized_batches.append([call.get("name", "") for call in tool_calls])
        return list(tool_calls), []

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)
    monkeypatch.setattr(subagent_module, "ToolRegistry", FakeToolRegistry)

    output = await subagent_module.run_subagent(
        AgentDef(
            name="explore",
            description="test",
            when_to_use="test",
            tools=["todo"],
            can_write=False,
            can_delegate=False,
        ),
        "Inspect child path",
        None,
        "test-key",
        Config(workspace=str(tmp_path)),
        runtime_persona="explore",
        max_steps=6,
        authorize_tools=authorize,
        debug=False,
    )

    assert "Runtime guard stopped this turn" in output
    assert executed_tools == ["todo", "todo"]
    assert authorized_batches == [["todo"], ["todo"]]


@pytest.mark.asyncio
async def test_subagent_todo_updates_sink_with_current_tool_message(tmp_path, monkeypatch):
    import voidx.agent.graph.subagent as subagent_module

    stream_calls: list[list] = []
    todo_states: list[object] = []

    class FakeModel:
        def bind_tools(self, _tool_defs):
            return self

    async def fake_stream_llm(_model, messages, _renderer, _protocol):
        stream_calls.append(list(messages))
        if len(stream_calls) == 1:
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": "todo",
                    "args": {"todos": [{"content": "inspect child path", "status": "in_progress"}]},
                    "id": "call_todo",
                    "type": "tool_call",
                }],
            )
        return AIMessage(content="done")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    output = await subagent_module.run_subagent(
        AgentDef(
            name="explore",
            description="test",
            when_to_use="test",
            tools=["todo"],
            can_write=False,
            can_delegate=False,
        ),
        "Inspect the workspace",
        None,
        "test-key",
        Config(workspace=str(tmp_path)),
        max_steps=4,
        parent_tools=ToolRegistry(),
        todo_state_sink=todo_states.append,
        debug=False,
    )

    assert output == "done"
    assert len(todo_states) == 1
    assert todo_states[0].items[0].content == "inspect child path"
    second_call_messages = stream_calls[1]
    assert any(
        isinstance(message, ToolMessage) and message.tool_call_id == "call_todo"
        for message in second_call_messages
    )


@pytest.mark.asyncio
async def test_subagent_empty_todo_does_not_clear_parent_state(tmp_path, monkeypatch):
    import voidx.agent.graph.subagent as subagent_module

    todo_states: list[object] = []
    calls = 0

    class FakeModel:
        def bind_tools(self, _tool_defs):
            return self

    async def fake_stream_llm(_model, _messages, _renderer, _protocol):
        nonlocal calls
        calls += 1
        if calls == 1:
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": "todo",
                    "args": {"todos": []},
                    "id": "call_todo",
                    "type": "tool_call",
                }],
            )
        return AIMessage(content="done")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    output = await subagent_module.run_subagent(
        AgentDef(
            name="explore",
            description="test",
            when_to_use="test",
            tools=["todo"],
            can_write=False,
            can_delegate=False,
        ),
        "Inspect the workspace",
        None,
        "test-key",
        Config(workspace=str(tmp_path)),
        max_steps=4,
        parent_tools=ToolRegistry(),
        todo_state_sink=todo_states.append,
        debug=False,
    )

    assert output == "done"
    assert todo_states == []


@pytest.mark.asyncio
async def test_subagent_skill_context_matches_orchestrator(tmp_path, monkeypatch):
    from voidx.agent.agents import get_agent
    import voidx.agent.graph.subagent as subagent_module

    captured: dict[str, list] = {}

    class FakeModel:
        def bind_tools(self, _tool_defs):
            return self

    async def fake_stream_llm(_model, messages, _renderer, _protocol):
        captured["messages"] = messages
        return AIMessage(content="done")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)
    workflow_context = await InstructionService(str(tmp_path)).workflow_context_for(
        "Implement the feature",
        agent="implement",
        task_intent="coding",
        goal_type="feature",
        interaction_mode=InteractionMode.AUTO.value,
        scope="Implement the feature",
    )

    output = await subagent_module.run_subagent(
        get_agent("voidx"),
        "Implement the feature",
        None,
        "test-key",
        Config(workspace=str(tmp_path)),
        runtime_persona="implement",
        max_steps=4,
        workflow_runtime_context=workflow_context,
        debug=False,
    )

    assert output == "done"
    workflow_context_messages = [
        message for message in captured["messages"]
        if isinstance(message, HumanMessage)
        and str(message.content).startswith(WORKFLOW_CONTEXT_MARKER)
    ]
    task_messages = [
        message for message in captured["messages"]
        if isinstance(message, HumanMessage)
        and "Runtime State" in str(message.content)
    ]
    assert len(workflow_context_messages) == 1
    assert len(task_messages) == 1
    assert "Workflow Node: brainstorm" in workflow_context_messages[0].content
    assert "Workflow Node: tdd" in workflow_context_messages[0].content
    assert "Workflow Node: verify" in workflow_context_messages[0].content
    assert "Workflow Node: tdd" not in task_messages[0].content
    assert "Active workflow nodes: brainstorm" in task_messages[0].content


@pytest.mark.asyncio
async def test_subagent_without_mcp_tools_excludes_parent_mcp_tools(tmp_path, monkeypatch):
    import voidx.agent.graph.subagent as subagent_module

    captured: dict[str, list] = {}

    class FakeModel:
        def bind_tools(self, tool_defs):
            captured["tool_defs"] = tool_defs
            return self

    async def fake_stream_llm(_model, _messages, _renderer, _protocol):
        return AIMessage(content="done")

    parent_tools = ToolRegistry()
    parent_tools.register(
        "mcp__demo__send_message_12345678",
        object(),
        "MCP demo",
        {"type": "object", "properties": {}},
    )

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    output = await subagent_module.run_subagent(
        AgentDef(
            name="explore",
            description="test",
            when_to_use="test",
            tools=["read"],
            can_write=False,
            can_delegate=False,
        ),
        "Inspect the workspace",
        None,
        "test-key",
        Config(workspace=str(tmp_path)),
        max_steps=3,
        parent_tools=parent_tools,
        debug=False,
    )

    assert output == "done"
    tool_names = [tool["function"]["name"] for tool in captured["tool_defs"]]
    assert "read" in tool_names
    assert "mcp__demo__send_message_12345678" not in tool_names


@pytest.mark.asyncio
async def test_subagent_with_mcp_tools_copies_parent_mcp_tools(tmp_path, monkeypatch):
    import voidx.agent.graph.subagent as subagent_module

    captured: dict[str, list] = {}
    calls: list[dict] = []

    class FakeModel:
        def bind_tools(self, tool_defs):
            captured["tool_defs"] = tool_defs
            return self

    class FakeMcpTool:
        async def execute(self, args, _ctx):
            calls.append(args)
            return ToolResult(output="mcp result")

    stream_count = 0

    async def fake_stream_llm(_model, _messages, _renderer, _protocol):
        nonlocal stream_count
        stream_count += 1
        if stream_count == 1:
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": "mcp__demo__send_message_12345678",
                    "args": {"text": "hello"},
                    "id": "mcp1",
                    "type": "tool_call",
                }],
            )
        return AIMessage(content="done")

    parent_tools = ToolRegistry()
    parent_tools.register(
        "mcp__demo__send_message_12345678",
        FakeMcpTool(),
        "MCP demo",
        {"type": "object", "properties": {"text": {"type": "string"}}},
    )

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    output = await subagent_module.run_subagent(
        AgentDef(
            name="explore",
            description="test",
            when_to_use="test",
            tools=["read"],
            can_write=False,
            can_delegate=False,
            mcp_tools=True,
        ),
        "Send the message",
        None,
        "test-key",
        Config(workspace=str(tmp_path)),
        max_steps=4,
        parent_tools=parent_tools,
        debug=False,
    )

    assert output == "done"
    tool_names = [tool["function"]["name"] for tool in captured["tool_defs"]]
    assert "mcp__demo__send_message_12345678" in tool_names
    assert calls == [{"text": "hello"}]


@pytest.mark.asyncio
async def test_subagent_tool_filter_always_blocks_nested_agent_tool(tmp_path, monkeypatch):
    import voidx.agent.graph.subagent as subagent_module

    captured: list[list[str]] = []

    class FakeModel:
        def bind_tools(self, tool_defs):
            captured.append([tool["function"]["name"] for tool in tool_defs])
            return self

    async def fake_stream_llm(_model, _messages, _renderer, _protocol):
        return AIMessage(content="done")

    parent_tools = ToolRegistry()
    parent_tools.register(
        "agent",
        object(),
        "Agent demo",
        {"type": "object", "properties": {}},
    )

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    for can_delegate in (False, True):
        output = await subagent_module.run_subagent(
            AgentDef(
                name="explore",
                description="test",
                when_to_use="test",
                tools=["read", "agent", "task_status"],
                can_write=False,
                can_delegate=can_delegate,
            ),
            "Inspect the workspace",
            None,
            "test-key",
            Config(workspace=str(tmp_path)),
            max_steps=3,
            parent_tools=parent_tools,
            debug=False,
        )
        assert output == "done"

    assert "agent" not in captured[0]
    assert "task_status" in captured[0]
    assert "agent" not in captured[1]
    assert "task_status" in captured[1]


@pytest.mark.asyncio
async def test_subagent_starts_from_isolated_task_context(tmp_path, monkeypatch):
    import voidx.agent.graph.subagent as subagent_module

    captured: dict[str, list] = {}

    class FakeModel:
        def bind_tools(self, _tool_defs):
            return self

    async def fake_stream_llm(_model, messages, _renderer, _protocol):
        captured["messages"] = messages
        return AIMessage(content="done")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    inherited_messages = [HumanMessage(content="Parent request")]
    RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
        skill_context_content=render_skill_context(["Skill instructions from: parent\nSkill: parent"]),
        current_user_text="Parent request",
    ).build().apply_to_messages(inherited_messages)
    assert inherited_messages[1].content.startswith(SKILL_CONTEXT_MARKER)
    workflow_context = await InstructionService(str(tmp_path)).workflow_context_for(
        "Inspect the workspace",
        agent="explore",
        task_intent="coding",
        goal_type="inspect",
        interaction_mode=InteractionMode.AUTO.value,
        scope="Inspect the workspace",
    )

    output = await subagent_module.run_subagent(
        get_agent("voidx"),
        "Inspect the workspace",
        None,
        "test-key",
        Config(workspace=str(tmp_path)),
        runtime_persona="explore",
        max_steps=4,
        workflow_runtime_context=workflow_context,
        debug=False,
    )

    assert output == "done"
    human_messages = [message for message in captured["messages"] if isinstance(message, HumanMessage)]
    workflow_context_messages = [
        message for message in human_messages
        if str(message.content).startswith(WORKFLOW_CONTEXT_MARKER)
    ]
    semantic_human_messages = [
        message for message in human_messages
        if not str(message.content).startswith((SKILL_CONTEXT_MARKER, WORKFLOW_CONTEXT_MARKER))
        and not is_step_hint_message(message)
    ]
    assert len(workflow_context_messages) == 1
    assert "Skill instructions from: parent" not in workflow_context_messages[0].content
    assert len(semantic_human_messages) == 1
    assert "Parent request" not in semantic_human_messages[0].content
    assert "Inspect the workspace" in semantic_human_messages[0].content
    assert "Runtime State" in semantic_human_messages[0].content


@pytest.mark.asyncio
async def test_subagent_adds_last_tool_step_hint_to_payload_only(tmp_path, monkeypatch):
    import voidx.agent.graph.subagent as subagent_module

    captured: dict[str, list] = {}
    sub_messages: list = []

    class FakeModel:
        def bind_tools(self, _tool_defs):
            return self

    async def fake_stream_llm(_model, messages, _renderer, _protocol):
        captured["messages"] = messages
        return AIMessage(content="done")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    output = await subagent_module.run_subagent(
        AgentDef(
            name="explore",
            description="test",
            when_to_use="test",
            tools=["fake_tool"],
            can_write=False,
            can_delegate=False,
        ),
        "Inspect the workspace",
        None,
        "test-key",
        Config(workspace=str(tmp_path)),
        max_steps=3,
        sub_messages=sub_messages,
        debug=False,
    )

    assert output == "done"
    assert captured["messages"][-1].content.startswith("[Step 1/3]")
    assert "LAST step with tools" in captured["messages"][-1].content
    assert is_step_hint_message(captured["messages"][-1])
    assert not any(is_step_hint_message(message) for message in sub_messages)


@pytest.mark.asyncio
async def test_subagent_final_step_fallback_does_not_leak_hint_to_sub_messages(tmp_path, monkeypatch):
    import voidx.agent.graph.subagent as subagent_module

    captured_calls: list[list] = []
    sub_messages: list = []

    class FakeModel:
        def bind_tools(self, _tool_defs):
            return self

    class FakeToolRegistry:
        def filtered_copy(self, _allowed_ids):
            return self

        def tools_for_llm(self):
            return [{
                "type": "function",
                "function": {
                    "name": "fake_tool",
                    "description": "fake",
                    "parameters": {"type": "object", "properties": {}},
                    "strict": True,
                },
            }]

        async def execute_tool(self, _tool_id, _args, _ctx):
            return ToolResult(output="read src/voidx/agent/graph/subagent.py")

    async def fake_stream_llm(_model, messages, _renderer, _protocol):
        captured_calls.append(messages)
        if len(captured_calls) == 1:
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": "fake_tool",
                    "args": {},
                    "id": "tc1",
                    "type": "tool_call",
                }],
            )
        return AIMessage(content="")

    monkeypatch.setattr(subagent_module, "ToolRegistry", FakeToolRegistry)
    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    output = await subagent_module.run_subagent(
        AgentDef(
            name="explore",
            description="test",
            when_to_use="test",
            tools=["fake_tool"],
            can_write=False,
            can_delegate=False,
        ),
        "Inspect the workspace",
        None,
        "test-key",
        Config(workspace=str(tmp_path)),
        max_steps=3,
        sub_messages=sub_messages,
        debug=False,
    )

    assert "Step limit reached: 2/3." in output
    assert "Goal: Inspect the workspace" in output
    assert "src/voidx/agent/graph/subagent.py" in output
    assert captured_calls[0][-1].content.startswith("[Step 1/3]")
    assert "LAST step with tools" in captured_calls[0][-1].content
    assert captured_calls[1][-1].content.startswith("[Step 2/3]")
    assert "FINAL response step" in captured_calls[1][-1].content
    assert not any(is_step_hint_message(message) for message in sub_messages)
