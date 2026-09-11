"""Child message protocol is narrowed to the terminal result channel (Plan A).

The child-facing message tool only exposes ``message(result)``; plain
message/question/answer stay in the transport/domain layer but are not
available to the child LLM, and forged non-result calls are hard-rejected.
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage

from tests.tool_registry import build_registry
from voidx.agent.adapters.langgraph.runtime import subagent as subagent_module
from voidx.agent.adapters.langgraph.runtime.subagent import run_subagent
from voidx.agent.adapters.subagent import InProcessSubagentGateway
from voidx.agent.adapters.tools.context import AgentToolExecutionContext, AgentToolRuntime
from voidx.agent.adapters.tools.subagent import AgentResultContract
from voidx.agent.adapters.tools.subagent_message import MessageTool
from voidx.agent.application.agents import AgentDef

from voidx.agent.domain.task.state import (

    GoalResolution,
    GoalSpec,
    PlanResolution,
)
from voidx.config import Config


class _CapturingModel:
    def __init__(self) -> None:
        self.tool_defs: list[dict] = []

    def bind_tools(self, tool_defs):
        self.tool_defs = list(tool_defs)
        return self


class _FakeUi:
    def step_header(self, _persona):
        return None

    def print(self, _text=""):
        return None


class _FakeEvents:
    async def emit(self, _event):
        return None

    def emit_direct(self, _event):
        return None


class _FakeUiPort:
    ui = _FakeUi()
    events = _FakeEvents()
    console = object()

    def via_events(self):
        return False


def _goal_resolution() -> GoalResolution:
    return GoalResolution(
        goal=GoalSpec(desc="message protocol probe"),
        plan=PlanResolution(join="review", leave="review"),
    )


def _contract() -> AgentResultContract:
    return AgentResultContract(format="verdict=PASS|FAIL|NEEDS_CHANGE, findings, risks, next_actions")


def _message_def(tool_defs: list[dict]) -> dict:
    for item in tool_defs:
        if isinstance(item, dict) and item.get("function", {}).get("name") == "message":
            return item["function"]
    raise AssertionError("message tool not found in child surface")


@pytest.mark.asyncio
async def test_child_message_tool_schema_is_result_only(tmp_path, monkeypatch):
    model = _CapturingModel()

    async def fake_stream_llm(_model, _messages, _renderer, _protocol, **_kwargs):
        return AIMessage(content="done")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_a, **_k: model)
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    output = await run_subagent(
        AgentDef(name="voidx", description="test", when_to_use="test", can_write=False, can_delegate=False),
        "Probe the message tool schema",
        "test-key",
        Config(workspace=str(tmp_path)),
        goal_resolution=_goal_resolution(),
        result_contract=_contract(),
        parent_tools=build_registry(),
        debug=False,
        ui_port=_FakeUiPort(),
    )

    assert output == "done"
    message_def = _message_def(model.tool_defs)
    properties = message_def["parameters"].get("properties", {})
    action = properties.get("action", {})
    message_type = properties.get("message_type", {})
    assert action.get("enum") == ["send"] or action.get("const") == "send"
    assert message_type.get("enum") == ["result"] or message_type.get("const") == "result"
    assert "limit" not in properties
    assert "timeout" not in properties
    description = message_def.get("description", "")
    assert "question" not in description.lower()


@pytest.mark.asyncio
async def test_child_message_forged_question_is_rejected(tmp_path, monkeypatch):
    """A forged non-result message call must fail even though the schema hides it."""
    calls = 0

    async def fake_stream_llm(_model, _messages, _renderer, _protocol, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": "message",
                    "args": {"action": "send", "message_type": "question", "payload": "{}"},
                    "id": "forge-q",
                }],
            )
        return AIMessage(content="done")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_a, **_k: _CapturingModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-msg")
    seen_contents: list[str] = []

    original_execute = MessageTool.execute

    async def recording_execute(self, args, ctx):
        result = await original_execute(self, args, ctx)
        seen_contents.append(result.output)
        return result

    monkeypatch.setattr(MessageTool, "execute", recording_execute)

    async def runner(run_id: str) -> str:
        return await run_subagent(
            AgentDef(name="voidx", description="test", when_to_use="test", can_write=False, can_delegate=False),
            "Forge a question message",
            "test-key",
            Config(workspace=str(tmp_path)),
            goal_resolution=_goal_resolution(),
            result_contract=_contract(),
            debug=False,
            agent_gateway=gateway,
            agent_run_id=run_id,
            parent_tools=build_registry(),
            ui_port=_FakeUiPort(),
        )

    run = await gateway.spawn(
        session_id="session-msg",
        parent_run_id=root_id,
        agent_name="voidx",
        description="Forge a question",
        runner=runner,
    )
    run = await gateway.wait(requester_run_id=root_id, target_run_id=run.run_id, timeout=10)

    assert run.status == "completed"
    assert seen_contents, "message tool was never executed"
    assert any("result" in content and "only" in content.lower() for content in seen_contents), seen_contents


@pytest.mark.asyncio
async def test_child_message_result_still_terminates_run(tmp_path, monkeypatch):
    calls = 0

    async def fake_stream_llm(_model, _messages, _renderer, _protocol, **_kwargs):
        nonlocal calls
        calls += 1
        return AIMessage(
            content="",
            tool_calls=[{
                "name": "message",
                "args": {
                    "action": "send",
                    "message_type": "result",
                    "payload": json.dumps({"result": "findings: all good"}),
                },
                "id": "call-result",
            }],
        )

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_a, **_k: _CapturingModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-result")

    async def runner(run_id: str) -> str:
        return await run_subagent(
            AgentDef(name="voidx", description="test", when_to_use="test", can_write=False, can_delegate=False),
            "Return the terminal result",
            "test-key",
            Config(workspace=str(tmp_path)),
            goal_resolution=_goal_resolution(),
            result_contract=_contract(),
            debug=False,
            agent_gateway=gateway,
            agent_run_id=run_id,
            parent_tools=build_registry(),
            ui_port=_FakeUiPort(),
        )

    run = await gateway.spawn(
        session_id="session-result",
        parent_run_id=root_id,
        agent_name="voidx",
        description="Terminal result",
        runner=runner,
    )
    run = await gateway.wait(requester_run_id=root_id, target_run_id=run.run_id, timeout=10)

    assert run.status == "completed"
    assert "all good" in str((run.result or {}).get("result") or "")


@pytest.mark.asyncio
async def test_result_only_message_tool_unit_rejects_receive() -> None:
    tool = MessageTool(result_only=True)
    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-unit")
    ctx = AgentToolExecutionContext(
        workspace="/tmp",
        runtime=AgentToolRuntime(subagent_transport=gateway, run_id=root_id),
    )

    result = await tool.execute({"action": "receive"}, ctx)

    assert result.metadata.get("error") is True
    assert result.metadata.get("reason") == "result_only"


@pytest.mark.asyncio
async def test_result_only_message_tool_unit_sends_result() -> None:
    tool = MessageTool(result_only=True)
    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-unit-result")
    child = await gateway.spawn(
        session_id="session-unit-result",
        parent_run_id=root_id,
        agent_name="voidx",
        description="unit",
        runner=lambda _rid: _never(),
    )
    ctx = AgentToolExecutionContext(
        workspace="/tmp",
        runtime=AgentToolRuntime(subagent_transport=gateway, run_id=child.run_id),
    )

    result = await tool.execute(
        {"action": "send", "message_type": "result", "payload": json.dumps({"result": "ok"})},
        ctx,
    )

    assert result.metadata.get("error") is not True
    terminal = gateway.lookup_run(child.run_id)
    assert terminal is not None and terminal.status == "completed"
    assert (terminal.result or {}).get("result") == "ok"


async def _never() -> str:
    raise AssertionError("runner must not be invoked")


@pytest.mark.asyncio
async def test_child_bare_result_message_rejected_then_final_answer_reported(tmp_path, monkeypatch):
    """A bare message(result) must be rejected; the later final answer becomes the result."""
    calls = 0

    async def fake_stream_llm(_model, _messages, _renderer, _protocol, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": "message",
                    "args": {"action": "send", "message_type": "result", "payload": "{}"},
                    "id": "bare-result",
                }],
            )
        return AIMessage(content="verdict: PASS\nfindings: none")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_a, **_k: _CapturingModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-bare-result")
    seen_outputs: list[str] = []

    original_execute = MessageTool.execute

    async def recording_execute(self, args, ctx):
        result = await original_execute(self, args, ctx)
        seen_outputs.append(result.output)
        return result

    monkeypatch.setattr(MessageTool, "execute", recording_execute)

    async def runner(run_id: str) -> str:
        return await run_subagent(
            AgentDef(name="voidx", description="test", when_to_use="test", can_write=False, can_delegate=False),
            "Report via bare result then final answer",
            "test-key",
            Config(workspace=str(tmp_path)),
            goal_resolution=_goal_resolution(),
            result_contract=_contract(),
            debug=False,
            agent_gateway=gateway,
            agent_run_id=run_id,
            parent_tools=build_registry(),
            ui_port=_FakeUiPort(),
        )

    run = await gateway.spawn(
        session_id="session-bare-result",
        parent_run_id=root_id,
        agent_name="voidx",
        description="Bare result",
        runner=runner,
    )
    run = await gateway.wait(requester_run_id=root_id, target_run_id=run.run_id, timeout=10)

    assert run.status == "completed"
    assert seen_outputs, "message tool was never executed"
    assert any("payload" in output.lower() for output in seen_outputs), seen_outputs
    assert "verdict: PASS" in str((run.result or {}).get("result") or "")
    assert await gateway.receive(run_id=root_id, limit=10, timeout=0) == []
