"""Regression tests for core graph behavior."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage, ToolMessage

import voidx.memory.store as store

from voidx.agent.agents import AgentDef, BASE_SYSTEM_PROMPT, VOIDX_PROMPT, get_agent, persona_prompt_for_llm
from voidx.agent.graph.convergence import is_step_hint_message
from voidx.agent.graph.runtime import current_parent_tool_call_id
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
from voidx.workflow.runtime import WorkflowRunState, WorkflowRunStatus
from voidx.agent.task_state import TaskState, ToolStatePatch
from voidx.tools.base import ToolContext, ToolResult
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
        agent_name: str,
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


def test_voidx_persona_prompt_declares_core_rules():
    assert "Runtime workflow gates take precedence over persona prompts" in VOIDX_PROMPT
    assert "Subagents do not interact with the user" in VOIDX_PROMPT
    assert "Switch persona" not in VOIDX_PROMPT
    assert "implement persona" not in VOIDX_PROMPT


def test_base_system_prompt_registers_all_runtime_personas():
    for persona in ("coordinate", "explore", "plan", "implement", "review"):
        assert f"**{persona}**" in BASE_SYSTEM_PROMPT


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
    subagent = get_agent("sub-voidx")

    assert agent is not None
    assert subagent is not None
    assert {"write", "edit", "lsp_format"}.issubset(set(agent.tools))
    assert {"write", "edit", "lsp_format"}.issubset(set(subagent.tools))
    assert {"clarify", "plan_checkpoint", "load_skills"}.issubset(set(agent.tools))
    assert get_agent("explore") is None
    assert get_agent("plan") is None
    assert get_agent("implement") is None
    assert get_agent("review") is None
    for visible_agent in (get_agent("voidx"), get_agent("sub-voidx")):
        assert visible_agent is not None
        assert "load_skills" in visible_agent.tools
    assert agent.can_write is True


def test_tool_contract_labels_agent_identity_not_runtime_persona():
    agent = get_agent("voidx")
    subagent = get_agent("sub-voidx")

    assert agent is not None
    assert subagent is not None
    assert "- Agent identity: voidx" in agent.tool_contract
    assert "- Persona: voidx" not in agent.tool_contract
    assert "- Agent identity: sub-voidx" in subagent.tool_contract
    assert "- Persona: sub-voidx" not in subagent.tool_contract


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


def test_hidden_personas_have_registered_prompts():
    assert get_agent("compaction") is not None
    assert get_agent("compaction").persona_prompt != ""
    assert get_agent("title") is not None
    assert get_agent("title").persona_prompt != ""


def test_permission_decision_splits_readonly_and_implement_agents():
    service = PermissionService()

    assert service.decide("agent", "explore") == "ask"
    assert service.decide("agent", "sub-voidx") == "allow"
    assert service.decide("agent", "implement") == "ask"


@pytest.mark.asyncio
async def test_graph_authorization_auto_allows_readonly_agent(tmp_path):
    graph = _graph(tmp_path)
    graph._permission.approval_policy = "untrusted"
    approved, denied = await graph._authorize_tool_calls(
        [{"name": "agent", "args": {"agent": "sub-voidx", "persona": "explore"}, "id": "call_1"}],
        agent_name="voidx",
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
        [{"name": "agent", "args": {"agent": "sub-voidx", "persona": "implement"}, "id": "call_1"}],
        agent_name="voidx",
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
        agent_name="voidx",
        plan_mode=False,
        session_id="test",
    )

    assert approved == []
    assert len(denied) == 1
    assert "Permission denied" in denied[0][1]


@pytest.mark.asyncio
async def test_graph_authorization_blocks_write_by_active_workflow_gate(tmp_path):
    graph = _graph(tmp_path)
    graph._permission.approval_policy = "on-request"

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "write", "args": {"file_path": "app.py", "content": "x"}, "id": "call_1"}],
        agent_name="voidx",
        plan_mode=False,
        session_id="test",
        workflow_runs=[
            WorkflowRunState(name="brainstorm", status=WorkflowRunStatus.ACTIVE),
        ],
    )

    assert approved == []
    assert len(denied) == 1
    assert "Blocked by workflow gate" in denied[0][1]
    assert "brainstorm" in denied[0][1]


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
        agent_name="voidx",
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

    class FakeApp:
        def __init__(self):
            self.notices: list[str] = []

        async def ask_choice(self, _prompt, _choices, details=None):
            return "a"

        def set_notice(self, text: str) -> None:
            self.notices.append(text)

    app = FakeApp()
    graph._app = app

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "write", "args": {"file_path": "app.py", "content": "x"}, "id": "call_1"}],
        agent_name="voidx",
        runtime_persona="implement",
        plan_mode=False,
        session_id="test",
    )

    assert [tc["name"] for tc in approved] == ["write"]
    assert denied == []
    assert app.notices == []


@pytest.mark.asyncio
async def test_graph_on_request_auto_approves_need_ask_tools(tmp_path):
    graph = _graph(tmp_path)
    graph._permission.approval_policy = "on-request"

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "write", "args": {"file_path": "app.py", "content": "x"}, "id": "call_1"}],
        agent_name="voidx",
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
        agent_name="voidx",
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
        [{"name": "lsp_format", "args": {"file_path": "src/app.py"}, "id": "call_1"}],
        agent_name="voidx",
        plan_mode=True,
        session_id="test",
    )

    assert approved == []
    assert len(denied) == 1
    assert "BLOCKED by plan mode" in denied[0][1]


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
        get_agent("sub-voidx"),
        "Implement the feature",
        None,
        runtime_persona="implement",
    )

    assert result == "child result"
    assert captured["workflow_runtime_context"] is expected_context
    assert "skill_selection" not in captured
    assert ("parent" + "_messages") not in captured
    assert calls[0]["kwargs"]["agent"] == "implement"
    assert calls[0]["kwargs"]["task_intent"] == "coding"
    assert calls[0]["kwargs"]["goal_type"] == "feature"
    assert calls[0]["kwargs"]["scope"] == "Implement the feature"


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
        await kwargs["authorize_tools"]([], "plan")
        return "child result"

    graph._instruction.workflow_context_for = fake_workflow_context_for
    graph._authorize_tool_calls = fake_authorize
    monkeypatch.setattr(core_module, "_run_subagent", fake_run_subagent)

    result = await graph._subagent_runner(
        get_agent("sub-voidx"),
        "Plan the feature",
        None,
        runtime_persona="plan",
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
        agent_name="voidx",
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
        agent_name="voidx",
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
        agent_name="voidx",
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
        agent_name="voidx",
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
        agent_name="voidx",
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
        agent_name: str,
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
        agent_name: str,
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
        agent_name: str,
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
        agent_name: str,
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
        agent_name: str,
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
        agent_name: str,
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
            agent_name: str,
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
        agent_name: str,
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
        agent_name: str,
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
        assert result["messages"] == []
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
        agent_name: str,
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

    assert [message.tool_call_id for message in result["messages"]] == ["call_read"]
    assert result["messages"][0].content == "read output"
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
        agent_name: str,
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

    assert result["messages"] == []
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
        agent_name: str,
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
        agent_name: str,
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
        agent_name: str,
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
        agent_name: str,
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
        agent_name: str,
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
    assert isinstance(messages[2], HumanMessage)
    assert "Active workflow nodes: debug" in messages[2].content
    result_task_state = TaskState.model_validate(result["task_state"])
    assert [name for name in (result_task_state.workflow_runs or {})] == [
        "debug",
        "tdd",
        "verify",
    ]
    assert "Workflow run state: debug=active" in messages[2].content


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
        get_agent("sub-voidx"),
        "Implement the feature",
        None,
        "test-key",
        Config(
            workspace=str(tmp_path),
            user_profile=UserProfile(language="zh-CN", tone="direct"),
        ),
        runtime_persona="implement",
        workflow_runtime_context=workflow_context,
        debug=False,
    )

    assert output == "done"
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
async def test_subagent_todo_updates_sink_without_tool_message(tmp_path, monkeypatch):
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
            max_steps=4,
        ),
        "Inspect the workspace",
        None,
        "test-key",
        Config(workspace=str(tmp_path)),
        parent_tools=ToolRegistry(),
        todo_state_sink=todo_states.append,
        debug=False,
    )

    assert output == "done"
    assert len(todo_states) == 1
    assert todo_states[0].items[0].content == "inspect child path"
    second_call_messages = stream_calls[1]
    assert not any(
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
            max_steps=4,
        ),
        "Inspect the workspace",
        None,
        "test-key",
        Config(workspace=str(tmp_path)),
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
        get_agent("sub-voidx"),
        "Implement the feature",
        None,
        "test-key",
        Config(workspace=str(tmp_path)),
        runtime_persona="implement",
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
            max_steps=3,
        ),
        "Inspect the workspace",
        None,
        "test-key",
        Config(workspace=str(tmp_path)),
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
            max_steps=4,
            mcp_tools=True,
        ),
        "Send the message",
        None,
        "test-key",
        Config(workspace=str(tmp_path)),
        parent_tools=parent_tools,
        debug=False,
    )

    assert output == "done"
    tool_names = [tool["function"]["name"] for tool in captured["tool_defs"]]
    assert "mcp__demo__send_message_12345678" in tool_names
    assert calls == [{"text": "hello"}]


@pytest.mark.asyncio
async def test_subagent_tool_filter_respects_can_delegate(tmp_path, monkeypatch):
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
                max_steps=3,
            ),
            "Inspect the workspace",
            None,
            "test-key",
            Config(workspace=str(tmp_path)),
            parent_tools=parent_tools,
            debug=False,
        )
        assert output == "done"

    assert "agent" not in captured[0]
    assert "task_status" in captured[0]
    assert "agent" in captured[1]
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
        get_agent("sub-voidx"),
        "Inspect the workspace",
        None,
        "test-key",
        Config(workspace=str(tmp_path)),
        runtime_persona="explore",
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
            max_steps=3,
        ),
        "Inspect the workspace",
        None,
        "test-key",
        Config(workspace=str(tmp_path)),
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
            max_steps=3,
        ),
        "Inspect the workspace",
        None,
        "test-key",
        Config(workspace=str(tmp_path)),
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
