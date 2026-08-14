from tests.tool_registry import build_registry
import asyncio
import pytest
from langchain_core.messages import AIMessage

from voidx.agent.application.agents import AgentDef
from voidx.agent.adapters.subagent import InProcessSubagentGateway
from voidx.agent.adapters.langgraph.runtime.subagent import run_subagent
from voidx.config import Config
from voidx.agent.domain.task.state import GoalResolution, GoalSpec, IntentResolution, PlanResolution
from voidx.agent.domain.task.intent import TaskIntent
from voidx.agent.adapters.tools.subagent import AgentResultContract
from voidx.tooling.domain.result import ToolResult


class FakeModel:
    def bind_tools(self, _tool_defs):
        return self


class FakeUi:
    def step_header(self, _persona):
        return None

    def print(self, _text=""):
        return None


class FakeEvents:
    async def emit(self, _event):
        return None

    def emit_direct(self, _event):
        return None


class FakeUiPort:
    ui = FakeUi()
    events = FakeEvents()
    console = object()

    def via_events(self):
        return False


def _goal_resolution() -> GoalResolution:
    return GoalResolution(
        intent=IntentResolution(type=TaskIntent.CODING),
        goal=GoalSpec(desc="Gateway result channel"),
        plan=PlanResolution(join="review", leave="review"),
    )


def _result_contract() -> AgentResultContract:
    return AgentResultContract(
        format="verdict=PASS|FAIL|NEEDS_CHANGE, findings, risks, next_actions",
    )


def _agent_def() -> AgentDef:
    return AgentDef(
        name="voidx",
        description="test child agent",
        when_to_use="test",
        can_write=False,
        can_delegate=False,
    )


@pytest.mark.asyncio
async def test_run_subagent_explicit_result_message_completes_gateway_run(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module

    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-1")

    async def fake_stream_llm(_model, _messages, _renderer, _protocol, **kwargs):
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "message",
                    "args": {
                        "action": "send",
                        "message_type": "result",
                        "payload": {"result": "explicit result", "verdict": "PASS"},
                    },
                    "id": "call-result",
                }
            ],
        )

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    async def runner(run_id: str) -> str:
        return await run_subagent(
            _agent_def(),
            "Review result channel",
            "test-key",
            Config(workspace=str(tmp_path)),
            runtime_persona="review",
            goal_resolution=_goal_resolution(),
            result_contract=_result_contract(),
            debug=False,
            agent_gateway=gateway,
            agent_run_id=run_id,
            ui_port=FakeUiPort(),
        )

    run = await gateway.spawn(
        session_id="session-1",
        parent_run_id=root_id,
        agent_name="voidx",
        description="Review result channel",
        runner=runner,
    )
    run = await gateway.wait(requester_run_id=root_id, target_run_id=run.run_id, timeout=1)
    messages = await gateway.receive(run_id=root_id, limit=10, timeout=0)

    assert run.status == "completed"
    assert run.result == {"result": "explicit result", "verdict": "PASS"}
    assert run.current_activity is None
    assert run.active_tools == []
    assert run.last_activity_at == run.updated_at
    assert [(message.type, message.payload) for message in messages] == [
        ("result", {"result": "explicit result", "verdict": "PASS"}),
        ("completed", {"run_id": run.run_id}),
    ]


@pytest.mark.asyncio
async def test_run_subagent_result_tool_call_suppresses_same_batch_followups(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module

    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-1")

    async def fake_stream_llm(_model, _messages, _renderer, _protocol, **kwargs):
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "message",
                    "args": {
                        "action": "send",
                        "message_type": "result",
                        "payload": {"result": "done"},
                    },
                    "id": "call-result",
                },
                {
                    "name": "message",
                    "args": {
                        "action": "send",
                        "message_type": "message",
                        "payload": {"text": "should not send"},
                    },
                    "id": "call-progress",
                },
            ],
        )

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    async def runner(run_id: str) -> str:
        return await run_subagent(
            _agent_def(),
            "Review result same batch",
            "test-key",
            Config(workspace=str(tmp_path)),
            runtime_persona="review",
            goal_resolution=_goal_resolution(),
            result_contract=_result_contract(),
            debug=False,
            agent_gateway=gateway,
            agent_run_id=run_id,
            ui_port=FakeUiPort(),
        )

    run = await gateway.spawn(
        session_id="session-1",
        parent_run_id=root_id,
        agent_name="voidx",
        description="Review result same batch",
        runner=runner,
    )
    run = await gateway.wait(requester_run_id=root_id, target_run_id=run.run_id, timeout=1)
    messages = await gateway.receive(run_id=root_id, limit=10, timeout=0)

    assert run.status == "completed"
    assert run.result == {"result": "done"}
    assert [(message.type, message.payload) for message in messages] == [
        ("result", {"result": "done"}),
        ("completed", {"run_id": run.run_id}),
    ]


@pytest.mark.asyncio
async def test_run_subagent_wraps_final_text_as_result_message(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module

    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-1")

    async def fake_stream_llm(_model, _messages, _renderer, _protocol, **kwargs):
        return AIMessage(content="fallback final result")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    async def runner(run_id: str) -> str:
        return await run_subagent(
            _agent_def(),
            "Review result fallback",
            "test-key",
            Config(workspace=str(tmp_path)),
            runtime_persona="review",
            goal_resolution=_goal_resolution(),
            result_contract=_result_contract(),
            debug=False,
            agent_gateway=gateway,
            agent_run_id=run_id,
            ui_port=FakeUiPort(),
        )

    run = await gateway.spawn(
        session_id="session-1",
        parent_run_id=root_id,
        agent_name="voidx",
        description="Review result fallback",
        runner=runner,
    )
    run = await gateway.wait(requester_run_id=root_id, target_run_id=run.run_id, timeout=1)
    messages = await gateway.receive(run_id=root_id, limit=10, timeout=0)

    assert run.status == "completed"
    assert run.result == {"result": "fallback final result"}
    assert [(message.type, message.payload) for message in messages] == [
        ("result", {"result": "fallback final result"}),
        ("completed", {"run_id": run.run_id}),
    ]



@pytest.mark.asyncio
async def test_run_subagent_registers_message_and_blocks_parent_only_tools(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module
    from voidx.agent.adapters.tools.subagent import AgentTool
    from voidx.agent.adapters.tools.interaction.checkpoint import PlanCheckpointTool
    from voidx.agent.adapters.tools.interaction.clarify import ClarifyTool
    from voidx.tooling.application.registry import ToolRegistry

    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-1")
    bound_tool_names: list[str] = []

    class CapturingModel(FakeModel):
        def bind_tools(self, tool_defs):
            bound_tool_names.clear()
            for item in tool_defs:
                function = item.get("function") if isinstance(item, dict) else None
                if isinstance(function, dict) and function.get("name"):
                    bound_tool_names.append(function["name"])
                elif isinstance(item, dict) and item.get("name"):
                    bound_tool_names.append(item["name"])
            return self

    async def fake_stream_llm(_model, _messages, _renderer, _protocol, **kwargs):
        return AIMessage(content="done")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: CapturingModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    parent_tools = build_registry()
    agent_tool = AgentTool(runner=None)
    parent_tools.register(agent_tool.id, agent_tool, agent_tool.description, agent_tool.parameters_schema())
    for tool in (ClarifyTool(), PlanCheckpointTool()):
        parent_tools.replace(tool.id, tool, tool.description, tool.parameters_schema())

    assert "message" not in parent_tools.ids()
    assert "agent" in parent_tools.ids()

    async def runner(run_id: str) -> str:
        return await run_subagent(
            _agent_def(),
            "Check child tool surface",
            "test-key",
            Config(workspace=str(tmp_path)),
            runtime_persona="review",
            goal_resolution=_goal_resolution(),
            result_contract=_result_contract(),
            debug=False,
            parent_tools=parent_tools,
            agent_gateway=gateway,
            agent_run_id=run_id,
            ui_port=FakeUiPort(),
        )

    run = await gateway.spawn(
        session_id="session-1",
        parent_run_id=root_id,
        agent_name="voidx",
        description="Check child tool surface",
        runner=runner,
    )
    run = await gateway.wait(requester_run_id=root_id, target_run_id=run.run_id, timeout=1)

    assert run.status == "completed"
    assert "message" in bound_tool_names
    assert "agent" not in bound_tool_names
    assert "clarify" not in bound_tool_names
    assert "checkpoint" not in bound_tool_names
    # child-only message must not leak into the parent registry
    assert "message" not in parent_tools.ids()


def test_root_tool_registry_does_not_register_message_by_default():
    from voidx.tooling.application.registry import ToolRegistry

    registry = build_registry()
    assert "message" not in registry.ids()


def test_partial_result_includes_findings_without_runtime_reason():
    from langchain_core.messages import HumanMessage, ToolMessage

    from voidx.agent.adapters.langgraph.runtime.subagent import _partial_result_from_messages

    messages = [
        HumanMessage(content="review the edit-window change"),
        AIMessage(content="关键发现：多 edit 倒序应用时 collapse_boundaries 会用旧行号。"),
        ToolMessage(content='{"ok": false, "blocked": true}', tool_call_id="c1", status="error"),
        AIMessage(content="", tool_calls=[{"name": "bash", "args": {"command": "python x.py"}, "id": "c1"}]),
    ]

    result = _partial_result_from_messages(messages)

    assert "关键发现" in result
    assert "task may be incomplete" in result
    assert "runtime" not in result.lower()
    assert "guard" not in result.lower()


@pytest.mark.asyncio
async def test_run_subagent_guard_terminated_returns_findings_fallback(tmp_path, monkeypatch):
    """Reproduces the Orion failure: every bash call is policy-blocked, the no-progress
    guard terminates, and the parent must still receive the child's findings."""
    import json as _json

    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module
    from voidx.tooling.domain.result import ToolResult
    from voidx.tooling.application.registry import ToolRegistry

    class FakeBashTool:
        id = "bash"
        description = "fake bash that is always policy-blocked"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, _args, _ctx):
            return ToolResult(
                output=_json.dumps({
                    "ok": False,
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": "shell policy deferred: nested interpreter",
                    "blocked": True,
                }),
                metadata={"blocked": True, "error": True},
            )

    parent_tools = build_registry()
    fake_bash = FakeBashTool()
    parent_tools.replace("bash", fake_bash, fake_bash.description, fake_bash.parameters_schema())

    calls = {"n": 0}

    async def fake_stream_llm(_model, _messages, _renderer, _protocol, **kwargs):
        calls["n"] += 1
        content = "关键发现：实现符合设计契约。" if calls["n"] == 1 else ""
        return AIMessage(
            content=content,
            tool_calls=[{"name": "bash", "args": {"command": f"python attempt{calls['n']}.py"}, "id": f"call-{calls['n']}"}],
        )

    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-1")
    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    run_metadata: dict[str, object] = {}

    async def runner(run_id: str) -> str:
        return await run_subagent(
            _agent_def(),
            "Review the change and verify by running a script",
            "test-key",
            Config(workspace=str(tmp_path)),
            runtime_persona="review",
            goal_resolution=_goal_resolution(),
            result_contract=_result_contract(),
            debug=False,
            agent_gateway=gateway,
            agent_run_id=run_id,
            ui_port=FakeUiPort(),
            parent_tools=parent_tools,
            run_metadata=run_metadata,
        )

    run = await gateway.spawn(
        session_id="session-1",
        parent_run_id=root_id,
        agent_name="voidx",
        description="Review with blocked verification",
        runner=runner,
    )
    run = await gateway.wait(requester_run_id=root_id, target_run_id=run.run_id, timeout=10)

    assert run.status == "completed"
    assert run_metadata.get("finish_reason") == "guard_terminated"
    result_text = str((run.result or {}).get("result") or "")
    assert "关键发现" in result_text
    assert "task may be incomplete" in result_text
    assert "runtime" not in result_text.lower()
    assert "guard" not in result_text.lower()


@pytest.mark.asyncio
async def test_run_subagent_reports_tool_activity_and_refreshes_child_context_each_step(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module

    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-activity-context")
    tool_started = asyncio.Event()
    release_tool = asyncio.Event()
    child_created = asyncio.Event()
    captured_prompts: list[str] = []
    model_activity_events: list[tuple] = []
    tool_start_events: list[tuple] = []
    original_start_model = gateway.start_model_activity
    original_touch_model = gateway.touch_model_activity
    original_finish_model = gateway.finish_model_activity
    original_start_tool = gateway.start_tool_activity

    def start_model(run_id: str, *, activity_id: str) -> None:
        model_activity_events.append(("start", run_id, activity_id))
        original_start_model(run_id, activity_id=activity_id)

    def touch_model(run_id: str, *, activity_id: str) -> None:
        model_activity_events.append(("touch", run_id, activity_id))
        original_touch_model(run_id, activity_id=activity_id)

    def finish_model(run_id: str, *, activity_id: str, succeeded: bool) -> None:
        model_activity_events.append(("finish", run_id, activity_id, succeeded))
        original_finish_model(run_id, activity_id=activity_id, succeeded=succeeded)

    def start_tool(run_id: str, **kwargs) -> None:
        tool_start_events.append((run_id, kwargs))
        original_start_tool(run_id, **kwargs)

    monkeypatch.setattr(gateway, "start_model_activity", start_model)
    monkeypatch.setattr(gateway, "touch_model_activity", touch_model)
    monkeypatch.setattr(gateway, "finish_model_activity", finish_model)
    monkeypatch.setattr(gateway, "start_tool_activity", start_tool)

    class BlockingTool:
        id = "blocking"
        description = "Block until the test releases the tool."

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, _args, _ctx):
            tool_started.set()
            await release_tool.wait()
            return ToolResult(output="tool done")

    parent_tools = build_registry()
    parent_tools.register_plugin(BlockingTool())
    calls = 0

    async def fake_stream_llm(_model, messages, _renderer, _protocol, **kwargs):
        nonlocal calls
        calls += 1
        captured_prompts.append("\n".join(str(message.content) for message in messages))
        on_activity = kwargs.get("on_activity")
        assert callable(on_activity)
        on_activity()
        if calls == 1:
            return AIMessage(
                content="",
                tool_calls=[{"name": "blocking", "args": {}, "id": "call-blocking"}],
            )
        assert child_created.is_set()
        return AIMessage(content="final result")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    async def runner(run_id: str) -> str:
        return await run_subagent(
            _agent_def(),
            "Review tool activity",
            "test-key",
            Config(workspace=str(tmp_path)),
            runtime_persona="review",
            goal_resolution=_goal_resolution(),
            result_contract=_result_contract(),
            debug=False,
            agent_gateway=gateway,
            agent_run_id=run_id,
            parent_tools=parent_tools,
            ui_port=FakeUiPort(),
        )

    parent = await gateway.spawn(
        session_id="session-activity-context",
        parent_run_id=root_id,
        agent_name="voidx",
        description="Goal: review activity",
        runner=runner,
    )
    await asyncio.wait_for(tool_started.wait(), timeout=1)
    running = gateway.lookup_run(parent.run_id)
    assert running is not None
    assert [(item.tool_name, item.status) for item in running.active_tools] == [
        ("blocking", "running"),
    ]

    nested_release = asyncio.Event()

    async def nested_runner(_run_id: str) -> str:
        await nested_release.wait()
        return "nested done"

    nested = await gateway.spawn(
        session_id="session-activity-context",
        parent_run_id=parent.run_id,
        agent_name="voidx",
        description="Goal: nested review",
        runner=nested_runner,
    )
    child_created.set()
    release_tool.set()

    parent = await gateway.wait(requester_run_id=root_id, target_run_id=parent.run_id, timeout=1)
    assert parent.status == "completed"
    assert parent.active_tools == []
    assert parent.last_tool is not None
    assert parent.last_tool.tool_name == "blocking"
    assert parent.last_tool.status == "succeeded"
    assert "Child agents: 1 running · 0 recent terminal" in captured_prompts[1]
    assert f"{nested.run_id} [running] Goal: nested review" in captured_prompts[1]
    starts = [event for event in model_activity_events if event[0] == "start"]
    touches = [event for event in model_activity_events if event[0] == "touch"]
    finishes = [event for event in model_activity_events if event[0] == "finish"]
    assert len(starts) == len(touches) == len(finishes)
    assert len(starts) >= 2
    activity_ids = [event[2] for event in starts]
    assert len(set(activity_ids)) == len(activity_ids)
    assert [event[2] for event in touches] == activity_ids
    assert [(event[2], event[3]) for event in finishes] == [
        (activity_id, True) for activity_id in activity_ids
    ]
    assert tool_start_events == [
        (
            parent.run_id,
            {
                "tool_name": "blocking",
                "tool_call_id": "call-blocking",
                "args": {},
                "workspace": str(tmp_path),
            },
        )
    ]

    nested_release.set()
    nested_task = gateway._runs[nested.run_id].task
    assert nested_task is not None
    await asyncio.wait_for(nested_task, timeout=1)
