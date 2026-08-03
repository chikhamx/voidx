import pytest
from langchain_core.messages import AIMessage

from voidx.agent.application.agents import AgentDef
from voidx.agent.gateway import AgentGateway
from voidx.agent.infrastructure.langgraph.runtime.subagent import run_subagent
from voidx.config import Config
from voidx.runtime import GoalResolution, GoalSpec, IntentResolution, PlanResolution, TaskIntent
from voidx.tools.agent import AgentResultContract


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
        schema_name="review_result",
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
    import voidx.agent.infrastructure.langgraph.runtime.subagent as subagent_module

    gateway = AgentGateway()
    root_id = gateway.ensure_root("session-1")

    async def fake_stream_llm(_model, _messages, _renderer, _protocol):
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
    assert [(message.type, message.payload) for message in messages] == [
        ("result", {"result": "explicit result", "verdict": "PASS"}),
        ("completed", {"run_id": run.run_id}),
    ]


@pytest.mark.asyncio
async def test_run_subagent_result_tool_call_suppresses_same_batch_followups(tmp_path, monkeypatch):
    import voidx.agent.infrastructure.langgraph.runtime.subagent as subagent_module

    gateway = AgentGateway()
    root_id = gateway.ensure_root("session-1")

    async def fake_stream_llm(_model, _messages, _renderer, _protocol):
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
                        "message_type": "progress",
                        "payload": {"step": "should not send"},
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
    import voidx.agent.infrastructure.langgraph.runtime.subagent as subagent_module

    gateway = AgentGateway()
    root_id = gateway.ensure_root("session-1")

    async def fake_stream_llm(_model, _messages, _renderer, _protocol):
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
    import voidx.agent.infrastructure.langgraph.runtime.subagent as subagent_module
    from voidx.tools.agent import AgentTool
    from voidx.tools.checkpoint import PlanCheckpointTool
    from voidx.tools.clarify import ClarifyTool
    from voidx.tools.registry import ToolRegistry

    gateway = AgentGateway()
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

    async def fake_stream_llm(_model, _messages, _renderer, _protocol):
        return AIMessage(content="done")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: CapturingModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    parent_tools = ToolRegistry()
    agent_tool = AgentTool(runner=None)
    parent_tools.register(agent_tool.id, agent_tool, agent_tool.description, agent_tool.parameters_schema())
    for tool in (ClarifyTool(), PlanCheckpointTool()):
        parent_tools.register(tool.id, tool, tool.description, tool.parameters_schema())

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
    from voidx.tools.registry import ToolRegistry

    registry = ToolRegistry()
    assert "message" not in registry.ids()


def test_guard_termination_result_includes_findings_and_blocker():
    from langchain_core.messages import HumanMessage, ToolMessage

    from voidx.agent.infrastructure.langgraph.runtime.subagent import _guard_termination_result

    messages = [
        HumanMessage(content="review the edit-window change"),
        AIMessage(content="关键发现：多 edit 倒序应用时 collapse_boundaries 会用旧行号。"),
        ToolMessage(content='{"ok": false, "blocked": true}', tool_call_id="c1", status="error"),
        AIMessage(content="", tool_calls=[{"name": "bash", "args": {"command": "python x.py"}, "id": "c1"}]),
    ]

    result = _guard_termination_result(messages, "No meaningful progress has been detected across 5 model/tool cycles.")

    assert result.startswith("No meaningful progress")
    assert "关键发现" in result
    assert "Blocker" in result


@pytest.mark.asyncio
async def test_run_subagent_guard_terminated_returns_findings_fallback(tmp_path, monkeypatch):
    """Reproduces the Orion failure: every bash call is policy-blocked, the no-progress
    guard terminates, and the parent must still receive the child's findings."""
    import json as _json

    import voidx.agent.infrastructure.langgraph.runtime.subagent as subagent_module
    from voidx.tools.base import BaseTool, ToolResult
    from voidx.tools.service import ToolRegistry

    class FakeBashTool(BaseTool):
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

    parent_tools = ToolRegistry()
    fake_bash = FakeBashTool()
    parent_tools.register("bash", fake_bash, fake_bash.description, fake_bash.parameters_schema())

    calls = {"n": 0}

    async def fake_stream_llm(_model, _messages, _renderer, _protocol):
        calls["n"] += 1
        content = "关键发现：实现符合设计契约。" if calls["n"] == 1 else ""
        return AIMessage(
            content=content,
            tool_calls=[{"name": "bash", "args": {"command": f"python attempt{calls['n']}.py"}, "id": f"call-{calls['n']}"}],
        )

    gateway = AgentGateway()
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
    assert "Blocker" in result_text
