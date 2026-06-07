"""Regression tests for core graph behavior."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage, ToolMessage

from voidx.agent.agents import AgentDef, get_agent, role_prompt_for_llm
from voidx.agent.graph.convergence import is_step_hint_message
from voidx.agent.graph.runtime import current_parent_tool_call_id
from voidx.agent.graph import VoidXGraph
from voidx.agent.graph.tool_execution import AGENT_RESULT_PREVIEW_CHARS, _agent_result_preview
from voidx.agent.message_rows import RowMessageCacheEntry
from voidx.agent.runtime_context import InteractionMode, RuntimeContextBuilder
from voidx.config import Config, ParallelSubagentsConfig, Settings, UserProfile
from voidx.llm.compaction import CompactionSelection
from voidx.llm.instruction import SkillRuntimeContext
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
from voidx.tools.base import ToolContext, ToolResult
from voidx.ui.output.dock import BottomInputDock, set_dock
from voidx.ui.output.events import DockEventConsumer, TurnStarted, ui_events


def _graph(tmp_path):
    cfg = Config(workspace=str(tmp_path))
    return VoidXGraph(cfg, api_key=None)


def test_agent_tool_result_preview_preserves_short_output():
    assert _agent_result_preview("short child conclusion\nsecond line") == "short child conclusion\nsecond line"


def test_agent_tool_result_preview_omits_extra_lines():
    output = "\n".join(f"child result line {index}" for index in range(1, 8))

    preview = _agent_result_preview(output)

    assert "child result line 1" in preview
    assert "child result line 5" in preview
    assert "child result line 6" not in preview
    assert "child result line 7" not in preview
    assert "... (2 more lines omitted; full result passed to orchestrator)" in preview


def test_agent_tool_result_preview_caps_long_single_line():
    output = "x" * (AGENT_RESULT_PREVIEW_CHARS + 17)

    preview = _agent_result_preview(output)

    assert preview.startswith("x" * AGENT_RESULT_PREVIEW_CHARS)
    assert len(preview.splitlines()[0]) == AGENT_RESULT_PREVIEW_CHARS
    assert "... (17 more chars omitted; full result passed to orchestrator)" in preview


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
            "agent": "orchestrator",
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
    assert "... (2 more lines omitted; full result passed to orchestrator)" in final_texts[0]
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
    assert "... (2 more lines omitted; full result passed to orchestrator)" in final_texts[0]


def test_graph_registers_agent_tool_not_task_tool(tmp_path):
    graph = _graph(tmp_path)
    ids = graph.tools.ids()

    assert "agent" in ids
    assert "agent_parallel" not in ids
    assert "on_intent" in ids
    assert "clarify" in ids
    assert "plan_checkpoint" in ids
    assert "task" not in ids


def test_agent_parallel_tool_not_registered_when_disabled(tmp_path):
    graph = _graph(tmp_path)

    assert "agent_parallel" not in graph.tools.ids()


def test_parallel_subagents_disabled_prompt_hides_capability():
    agent = get_agent("orchestrator")
    assert agent is not None

    prompt = role_prompt_for_llm(agent, parallel_subagents_enabled=False)

    assert "Delegate at most one child agent in a response" in prompt
    assert "multiple `agent` tool calls" not in prompt
    assert "run concurrently" not in prompt


def test_parallel_subagents_enabled_prompt_exposes_capability():
    agent = get_agent("orchestrator")
    assert agent is not None

    prompt = role_prompt_for_llm(agent, parallel_subagents_enabled=True)

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


def test_graph_session_date_uses_session_creation_date(tmp_path):
    session = SessionInfo(
        id="s1",
        workspace=str(tmp_path),
        created_at="2026-06-06T12:00:00",
        updated_at="2026-06-07T12:00:00",
    )

    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None, session=session)

    assert graph._session_date.startswith("2026-06-06 ")


def test_on_intent_schema_inlines_task_intent_enum(tmp_path):
    graph = _graph(tmp_path)
    schema = graph.tools.get_def("on_intent").parameters

    assert "$ref" not in schema["properties"]["intent"]
    assert schema["properties"]["intent"]["enum"] == [
        "chat",
        "inspect",
        "design",
        "review",
        "implement",
        "debug",
        "ambiguous",
    ]


def test_orchestrator_has_direct_edit_tools():
    agent = get_agent("orchestrator")
    implement = get_agent("implement")

    assert agent is not None
    assert implement is not None
    assert {"write", "edit", "apply_patch", "lsp_format"}.issubset(set(agent.tools))
    assert {"write", "edit", "apply_patch", "lsp_format"}.issubset(set(implement.tools))
    assert {"clarify", "plan_checkpoint"}.issubset(set(agent.tools))
    assert agent.can_write is True


def test_role_prompt_rejects_unregistered_agent_name():
    agent = AgentDef(
        name="orchesrator",
        description="typo",
        when_to_use="never",
        tools=[],
        can_write=False,
        can_delegate=False,
    )

    with pytest.raises(ValueError, match="No role prompt registered"):
        _ = agent.role_prompt


def test_promptless_builtin_agents_are_explicit():
    agent = get_agent("compaction")

    assert agent is not None
    assert agent.role_prompt == ""


def test_permission_decision_splits_readonly_and_implement_agents():
    service = PermissionService()

    assert service.decide("agent", "explore") == "allow"
    assert service.decide("agent", "implement") == "ask"


@pytest.mark.asyncio
async def test_graph_authorization_auto_allows_readonly_agent(tmp_path):
    graph = _graph(tmp_path)
    graph._permission.approval_policy = "untrusted"
    approved, denied = await graph._authorize_tool_calls(
        [{"name": "agent", "args": {"agent": "explore"}, "id": "call_1"}],
        agent_name="orchestrator",
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
        [{"name": "agent", "args": {"agent": "implement"}, "id": "call_1"}],
        agent_name="orchestrator",
        plan_mode=False,
        session_id="test",
    )

    assert [tc["name"] for tc in approved] == ["agent"]
    assert denied == []
    assert [[tc["args"]["agent"] for tc in batch] for batch in asked] == [["implement"]]


@pytest.mark.asyncio
async def test_graph_authorization_respects_session_deny_for_safe_bash(tmp_path):
    graph = _graph(tmp_path)
    graph._permission.deny_silent("bash")

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "bash", "args": {"command": "ls"}, "id": "call_1"}],
        agent_name="orchestrator",
        plan_mode=False,
        session_id="test",
    )

    assert approved == []
    assert len(denied) == 1
    assert "Permission denied" in denied[0][1]


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
        agent_name="orchestrator",
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
        agent_name="orchestrator",
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
        agent_name="orchestrator",
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
async def test_graph_authorization_blocks_lsp_format_in_plan_mode(tmp_path):
    graph = _graph(tmp_path)

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "lsp_format", "args": {"file_path": "src/app.py"}, "id": "call_1"}],
        agent_name="orchestrator",
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

    async def empty_skill_context(*_args, **_kwargs):
        return SkillRuntimeContext(instructions=[], active=[])

    graph._instruction.system = empty_system
    graph._instruction.skill_context_for = empty_skill_context

    messages = [HumanMessage(content="给个方案")]
    await graph._prepare_with_stream({
        "messages": messages,
        "workspace": str(tmp_path),
        "plan_mode": True,
        "agent": "orchestrator",
    })

    assert isinstance(messages[0], SystemMessage)
    assert "## Mode Prompt" in messages[0].content
    assert "## PLAN MODE ACTIVE" in messages[0].content


@pytest.mark.asyncio
async def test_graph_authorization_does_not_treat_goal_as_read_only_mode(tmp_path):
    graph = _graph(tmp_path)
    graph._permission.approval_policy = "on-request"

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "edit", "args": {"file_path": "src/app.py"}, "id": "call_1"}],
        agent_name="orchestrator",
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
        agent_name="orchestrator",
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
        agent_name="orchestrator",
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
        agent_name="orchestrator",
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
        agent_name="orchestrator",
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
        "agent": "orchestrator",
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
        "agent": "orchestrator",
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
        "agent": "orchestrator",
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
        "agent": "orchestrator",
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
        "agent": "orchestrator",
        "plan_mode": False,
    })

    messages = result["messages"]
    assert messages[0].tool_call_id == "call_a"
    assert "Tool execution error: boom" in messages[0].content
    assert messages[1].tool_call_id == "call_b"
    assert messages[1].content == "done call_b"


@pytest.mark.asyncio
async def test_parallel_subagents_keeps_barrier_deferral(tmp_path):
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
            return ToolResult(output="should not run")

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
        "agent": "orchestrator",
        "plan_mode": False,
    })

    assert executed == ["plan_checkpoint"]
    assert [msg.tool_call_id for msg in result["messages"]] == ["call_plan", "call_agent"]
    assert result["messages"][0].content == "checkpoint ok"
    assert "Deferred until after a runtime barrier tool" in result["messages"][1].content


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
        "agent": "orchestrator",
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
                "agent": "orchestrator",
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
async def test_execute_tools_keeps_parallel_child_agent_buffers_isolated(tmp_path):
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
            graph._sub_buffers.setdefault(call_id, []).append(AIMessage(content=f"sub {call_id}"))
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
        "agent": "orchestrator",
        "plan_mode": False,
    })

    messages = result["messages"]
    assert [msg.tool_call_id for msg in messages[:2] if isinstance(msg, ToolMessage)] == ["call_a", "call_b"]
    assert [msg.content for msg in messages[2:]] == ["sub call_a", "sub call_b"]


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
            "agent": "orchestrator",
            "plan_mode": False,
        })
        await ui_events.drain()

        assistant = next(node for node in test_dock.tree.root.children if node.node_type == "assistant")
        todo = next(node for node in assistant.children if node.node_type == "todo")

        assert todo.payload["items"] == [{"content": "wire event", "status": "in_progress"}]
        assert todo.payload["summary"] == "0/1 done · 1 active · 0 pending"
        assert [message.tool_call_id for message in result["messages"] if isinstance(message, ToolMessage)] == ["call_todo"]
    finally:
        await ui_events.stop()
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)


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
            "agent": "orchestrator",
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
        assert "... (2 more lines omitted; full result passed to orchestrator)" in final_result_text
        assert "child hidden thought" not in rendered
        assert "child final line 7" not in rendered
        tool_messages = [message for message in result["messages"] if isinstance(message, ToolMessage)]
        assert tool_messages[0].tool_call_id == "call_agent"
        assert tool_messages[0].content == child_output
        assert any(isinstance(message, AIMessage) and message.content == child_output for message in result["messages"])
    finally:
        await ui_events.stop()
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)


@pytest.mark.asyncio
async def test_execute_tools_applies_on_intent_state_patch(tmp_path):
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
        tool_calls=[
            {
                "name": "on_intent",
                "args": {
                    "intent": "implement",
                    "confidence": 0.92,
                    "reason": "user asked to implement the approved design",
                    "scope": "实现 on_intent runtime callback",
                },
                "id": "call_intent",
                "type": "tool_call",
            }
        ],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "agent": "orchestrator",
        "plan_mode": False,
        "interaction_mode": "auto",
        "task_intent": "chat",
    })

    assert result["task_intent"] == "implement"
    assert result["pending_approval"] is None
    assert result["intent_source"] == "on_intent"
    assert result["intent_refined"] is True
    assert "write" in result["available_tool_ids"]
    assert {
        run.name for run in result["skill_runs"]
    } >= {"test-driven-development", "verification-before-completion"}
    assert "confirmed_intent" in result["messages"][0].content


@pytest.mark.asyncio
async def test_on_intent_downgrades_implementation_in_plan_mode(tmp_path):
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
        tool_calls=[
            {
                "name": "on_intent",
                "args": {
                    "intent": "implement",
                    "confidence": 0.95,
                    "reason": "model thinks this asks for code changes",
                    "scope": "设计 runtime callback",
                },
                "id": "call_intent",
                "type": "tool_call",
            }
        ],
    )

    result = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "agent": "orchestrator",
        "plan_mode": True,
        "interaction_mode": "plan",
        "task_intent": "chat",
    })

    assert result["task_intent"] == "design"
    assert result["pending_approval"]["scope"] == "设计 runtime callback"
    assert "write" not in result["available_tool_ids"]
    assert "edit" not in result["available_tool_ids"]


@pytest.mark.asyncio
async def test_on_intent_defers_other_tools_in_same_batch(tmp_path):
    graph = _graph(tmp_path)

    class ExplodingReadTool:
        id = "read"
        description = "fake read"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            pytest.fail("read should be deferred until after on_intent")

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
            {
                "name": "on_intent",
                "args": {
                    "intent": "inspect",
                    "confidence": 0.84,
                    "reason": "needs repository inspection",
                    "scope": "检查 agent runtime",
                },
                "id": "call_intent",
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
        "agent": "orchestrator",
        "plan_mode": False,
        "interaction_mode": "auto",
        "task_intent": "chat",
    })

    assert [message.tool_call_id for message in result["messages"]] == ["call_intent", "call_read"]
    assert "Deferred until after a runtime barrier tool" in result["messages"][1].content


@pytest.mark.asyncio
async def test_plan_checkpoint_defers_other_tools_and_updates_state(tmp_path):
    graph = _graph(tmp_path)

    class ExplodingReadTool:
        id = "read"
        description = "fake read"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            pytest.fail("read should be deferred until after plan_checkpoint")

    graph.tools.register("read", ExplodingReadTool(), "fake read", {"type": "object", "properties": {}})

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
        "agent": "orchestrator",
        "plan_mode": False,
        "interaction_mode": "auto",
        "task_intent": "design",
        "pending_approval": {"kind": "implementation", "scope": "Update runtime state handling"},
    })

    assert [message.tool_call_id for message in result["messages"]] == ["call_plan", "call_read"]
    assert result["task_intent"] == "implement"
    assert result["goal"] == "Update runtime state handling"
    assert result["goal_phase"] == "implement"
    assert result["pending_approval"] is None
    assert "Deferred until after a runtime barrier tool" in result["messages"][1].content


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
        "agent": "orchestrator",
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
async def test_prepare_injects_matching_skill_instructions(tmp_path):
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
        "agent": "orchestrator",
        "plan_mode": False,
        "tool_results": {},
        "step_count": 0,
        "max_steps": 50,
        "should_continue": True,
    }

    await graph._prepare_with_stream(state)

    assert isinstance(messages[0], SystemMessage)
    assert isinstance(messages[1], HumanMessage)
    assert "Active Skills" in messages[1].content
    assert "Skill instructions from:" in messages[1].content
    assert "Skill: docs" in messages[1].content
    assert "Write concise docs." in messages[1].content


@pytest.mark.asyncio
async def test_prepare_injects_workflow_skills_from_task_state(tmp_path):
    graph = VoidXGraph(
        Config(workspace=str(tmp_path)),
        api_key=None,
        settings=Settings(str(tmp_path)),
    )
    messages = [HumanMessage(content="对，可以")]
    state = {
        "messages": messages,
        "workspace": str(tmp_path),
        "agent": "orchestrator",
        "plan_mode": False,
        "interaction_mode": "auto",
        "task_intent": "implement",
        "tool_results": {},
        "step_count": 0,
        "max_steps": 50,
        "should_continue": True,
    }

    result = await graph._prepare_with_stream(state)

    assert isinstance(messages[1], HumanMessage)
    assert "Skill: test-driven-development" in messages[1].content
    assert "Skill: verification-before-completion" in messages[1].content
    assert "Active workflow skills: test-driven-development" in messages[1].content
    assert [run.name for run in result["skill_runs"]] == [
        "test-driven-development",
        "verification-before-completion",
    ]
    assert "Skill run state: test-driven-development=active" in messages[1].content


@pytest.mark.asyncio
async def test_implement_subagent_injects_workflow_skills(tmp_path, monkeypatch):
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

    output = await subagent_module.run_subagent(
        get_agent("implement"),
        "Implement the feature",
        None,
        "test-key",
        Config(
            workspace=str(tmp_path),
            user_profile=UserProfile(language="zh-CN", tone="direct"),
        ),
        debug=False,
    )

    assert output == "done"
    rendered_user = next(
        message.content
        for message in captured["messages"]
        if isinstance(message, HumanMessage)
    )
    assert "Skill: test-driven-development" in rendered_user
    assert "Skill: verification-before-completion" in rendered_user
    assert "Active workflow skills: test-driven-development" in rendered_user
    assert "User language: Chinese (Simplified) [zh-CN]" in rendered_user
    assert "Tone instruction: Be direct and practical. Lead with the answer or action." in rendered_user


@pytest.mark.asyncio
async def test_subagent_parent_history_strips_parent_turn_overlay(tmp_path, monkeypatch):
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

    parent_messages = [HumanMessage(content="Parent request")]
    RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        agent_prompt="You are voidx.",
        agent="orchestrator",
        interaction_mode=InteractionMode.AUTO,
        skill_instructions=["Skill instructions from: parent\nSkill: parent"],
        current_user_text="Parent request",
    ).build().apply_to_messages(parent_messages)
    assert "Active Skills" in parent_messages[-1].content

    output = await subagent_module.run_subagent(
        get_agent("explore"),
        "Inspect the workspace",
        None,
        "test-key",
        Config(workspace=str(tmp_path)),
        parent_messages=parent_messages,
        debug=False,
    )

    assert output == "done"
    human_messages = [message for message in captured["messages"] if isinstance(message, HumanMessage)]
    assert len(human_messages) == 2
    assert human_messages[0].content == "Parent request"
    assert "Active Skills" not in human_messages[0].content
    assert "Runtime State" not in human_messages[0].content
    assert "Inspect the workspace" in human_messages[1].content
    assert "Runtime State" in human_messages[1].content


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
        def filter_tools(self, _allowed_ids):
            return None

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
