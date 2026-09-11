"""End-to-end child result handoff: spawn metadata keeps the delegation mode,
terminal results carry structured verdicts, and agent_control(wait) snapshots
drive the same review auto-advance event as the legacy sync path."""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage

from tests.tool_registry import build_registry
from voidx.agent.adapters.langgraph.runtime import subagent as subagent_module
from voidx.agent.adapters.langgraph.runtime.subagent import run_subagent
from voidx.agent.adapters.subagent import InProcessSubagentGateway
from voidx.agent.adapters.tools.context import AgentToolExecutionContext, AgentToolRuntime
from voidx.agent.adapters.tools.subagent import AgentTool
from voidx.agent.adapters.tools.subagent_control import AgentControlTool
from voidx.tooling.domain.result import ToolResult
from voidx.agent.application.agents import AgentDef
from voidx.agent.application.automation.workflow.auto_advance import auto_advance_events
from voidx.agent.domain.automation.workflow import WorkflowRunState, WorkflowRunStatus
from voidx.agent.domain.automation.workflow_dag import DEFAULT_WORKFLOW_DAG
from voidx.config import Config


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


class _FakeModel:
    def bind_tools(self, _tool_defs):
        return self


def _agent_def() -> AgentDef:
    return AgentDef(
        name="voidx",
        description="test",
        when_to_use="test",
        can_write=False,
        can_delegate=False,
    )


def _runtime(gateway: InProcessSubagentGateway, run_id: str) -> AgentToolRuntime:
    return AgentToolRuntime(subagent_transport=gateway, run_id=run_id)


def _review_events(tool_name: str, tool_result) -> list:
    runs = [WorkflowRunState(name="review", status=WorkflowRunStatus.ACTIVE)]
    return auto_advance_events(
        [{"name": tool_name, "result": tool_result}],
        workflow_runs=runs,
        dag=DEFAULT_WORKFLOW_DAG,
    )


async def _spawn_review_child(
    tmp_path,
    monkeypatch,
    *,
    verdict_text: str | None,
    runner_override=None,
):
    """Spawn a review child through the real AgentTool path; returns (gateway, tool_result)."""
    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-handoff")

    async def fake_stream_llm(_model, _messages, _renderer, _protocol, **_kwargs):
        return AIMessage(content=verdict_text or "")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_a, **_k: _FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    async def child_runner(
        agent_def,
        description,
        goal_resolution,
        result_contract,
        *,
        agent_run_id=None,
        agent_gateway=None,
        run_metadata=None,
    ):
        return await run_subagent(
            agent_def,
            description,
            "test-key",
            Config(workspace=str(tmp_path)),
            goal_resolution=goal_resolution,
            result_contract=result_contract,
            debug=False,
            parent_tools=build_registry(),
            ui_port=_FakeUiPort(),
            agent_gateway=agent_gateway,
            agent_run_id=agent_run_id,
            run_metadata=run_metadata,
        )

    tool = AgentTool(
        runner=runner_override or child_runner,
        agent_resolver=lambda _name: _agent_def(),
    )
    ctx = AgentToolExecutionContext(
        workspace=str(tmp_path),
        session_id="session-handoff",
        runtime=_runtime(gateway, root_id),
    )
    spawn_result = await tool.execute(
        {
            "mode": "review",
            "goal": "Review the change",
            "detail": "Check the implementation for correctness issues.",
        },
        ctx,
    )
    return gateway, root_id, spawn_result, ctx


@pytest.mark.asyncio
async def test_spawn_metadata_preserves_delegation_mode(tmp_path, monkeypatch):
    gateway, root_id, spawn_result, _ctx = await _spawn_review_child(
        tmp_path, monkeypatch, verdict_text="verdict: PASS\nfindings: none"
    )

    assert spawn_result.metadata.get("agent") == "voidx"
    assert spawn_result.metadata.get("mode") == "review"
    assert spawn_result.metadata.get("status") == "running"

    run_id = spawn_result.metadata["run_id"]
    run = gateway.lookup_run(run_id)
    assert run is not None and run.mode == "review"


@pytest.mark.asyncio
async def test_review_child_fail_via_wait_drives_review_has_issues(tmp_path, monkeypatch):
    gateway, root_id, spawn_result, ctx = await _spawn_review_child(
        tmp_path,
        monkeypatch,
        verdict_text="verdict: FAIL\nfindings: bug found\nrisks: none\nnext_actions: fix it",
    )
    run_id = spawn_result.metadata["run_id"]

    wait_result = await AgentControlTool().execute({"action": "wait", "run_id": run_id}, ctx)

    run_snapshot = wait_result.metadata.get("run") or {}
    assert run_snapshot.get("mode") == "review"
    assert run_snapshot.get("status") == "completed"
    result_payload = run_snapshot.get("result") or {}
    assert result_payload.get("verdict") == "FAIL"
    assert "bug found" in str(result_payload.get("result") or "")

    events = _review_events("agent_control", wait_result)
    assert len(events) == 1
    assert events[0].condition == "review_has_issues"


@pytest.mark.asyncio
async def test_review_child_pass_via_wait_produces_no_event(tmp_path, monkeypatch):
    gateway, root_id, spawn_result, ctx = await _spawn_review_child(
        tmp_path, monkeypatch, verdict_text="verdict: PASS\nfindings: none"
    )
    wait_result = await AgentControlTool().execute(
        {"action": "wait", "run_id": spawn_result.metadata["run_id"]}, ctx
    )

    assert (wait_result.metadata.get("run") or {}).get("status") == "completed"
    assert _review_events("agent_control", wait_result) == []


@pytest.mark.asyncio
async def test_failed_child_via_wait_produces_no_review_event(tmp_path, monkeypatch):
    async def failing_runner(*_args, **_kwargs):
        raise RuntimeError("child exploded")

    gateway, root_id, spawn_result, ctx = await _spawn_review_child(
        tmp_path, monkeypatch, verdict_text=None, runner_override=failing_runner
    )
    wait_result = await AgentControlTool().execute(
        {"action": "wait", "run_id": spawn_result.metadata["run_id"]}, ctx
    )

    assert (wait_result.metadata.get("run") or {}).get("status") == "failed"
    assert _review_events("agent_control", wait_result) == []


@pytest.mark.asyncio
async def test_terminal_result_has_mode_status_output_and_finish_reason(tmp_path, monkeypatch):
    gateway, _root_id, spawn_result, ctx = await _spawn_review_child(
        tmp_path,
        monkeypatch,
        verdict_text="verdict: FAIL\nfindings: bug found",
    )

    wait_result = await AgentControlTool().execute(
        {"action": "wait", "run_id": spawn_result.metadata["run_id"]}, ctx
    )
    run_snapshot = wait_result.metadata["run"]
    result_payload = run_snapshot["result"]

    assert result_payload["mode"] == "review"
    assert result_payload["status"] == "completed"
    assert result_payload["output"] == result_payload["result"]
    assert result_payload["verdict"] == "FAIL"
    assert result_payload["finish_reason"] == "final_answer"
    assert gateway.lookup_run(spawn_result.metadata["run_id"]).result == result_payload


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason", ["contract_unsatisfied", "context_limit", "guard_terminated"])
async def test_incomplete_terminal_result_is_not_completed_review(tmp_path, monkeypatch, finish_reason):
    async def incomplete_runner(*_args, **kwargs):
        kwargs["run_metadata"]["finish_reason"] = finish_reason
        return "verdict: FAIL\npartial finding"

    gateway, _root_id, spawn_result, ctx = await _spawn_review_child(
        tmp_path,
        monkeypatch,
        verdict_text=None,
        runner_override=incomplete_runner,
    )

    wait_result = await AgentControlTool().execute(
        {"action": "wait", "run_id": spawn_result.metadata["run_id"]}, ctx
    )
    run_snapshot = wait_result.metadata["run"]
    result_payload = run_snapshot["result"]

    assert result_payload["status"] == "incomplete"
    assert result_payload["output"] == result_payload["result"]
    assert result_payload["finish_reason"] == finish_reason
    assert _review_events("agent_control", wait_result) == []


@pytest.mark.asyncio
async def test_terminal_lifecycle_payload_contains_authoritative_envelope():
    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-terminal-envelope")

    async def runner(_run_id: str) -> str:
        return "verdict: PASS"

    child = await gateway.spawn(
        session_id="session-terminal-envelope",
        parent_run_id=root_id,
        agent_name="voidx",
        description="envelope",
        runner=runner,
        mode="review",
    )
    lifecycle = await gateway.receive(run_id=root_id, limit=1, timeout=1)

    assert lifecycle[0].type == "completed"
    assert lifecycle[0].payload["run_id"] == child.run_id
    assert lifecycle[0].payload["result"]["mode"] == "review"
    assert lifecycle[0].payload["result"]["status"] == "completed"
    assert lifecycle[0].payload["result"]["output"] == "verdict: PASS"

    waited = await gateway.wait(
        requester_run_id=root_id,
        target_run_id=child.run_id,
        timeout=1,
    )
    assert waited.status == "completed"
    assert await gateway.receive(run_id=root_id, limit=1, timeout=0) == []


@pytest.mark.asyncio
async def test_timeout_terminal_envelope_is_not_a_review_verdict():
    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-timeout-envelope")

    async def runner(_run_id: str) -> str:
        raise TimeoutError("child deadline exceeded")

    child = await gateway.spawn(
        session_id="session-timeout-envelope",
        parent_run_id=root_id,
        agent_name="voidx",
        description="timeout",
        runner=runner,
        mode="review",
    )
    wait = await gateway.wait(
        requester_run_id=root_id,
        target_run_id=child.run_id,
        timeout=1,
    )

    assert wait.status == "failed"
    assert wait.result["status"] == "timeout"
    assert wait.result["finish_reason"] == "timeout"
    assert _review_events(
        "agent_control",
        ToolResult(
            output="timeout",
            metadata={
                "run": wait.model_dump(mode="json"),
                "wait_outcome": wait.wait_outcome,
            },
        ),
    ) == []


@pytest.mark.asyncio
async def test_cancelled_terminal_envelope_is_not_completed():
    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-cancelled-envelope")
    release = asyncio.Event()

    async def runner(_run_id: str) -> str:
        await release.wait()
        return "late result"

    child = await gateway.spawn(
        session_id="session-cancelled-envelope",
        parent_run_id=root_id,
        agent_name="voidx",
        description="cancelled",
        runner=runner,
        mode="review",
    )
    cancelled = await gateway.cancel(
        requester_run_id=root_id,
        target_run_id=child.run_id,
    )

    assert cancelled.status == "cancelled"
    assert cancelled.result["status"] == "cancelled"
    assert cancelled.result["finish_reason"] == "cancelled"
    assert "verdict" not in cancelled.result

    release.set()
    await gateway.close_all()


@pytest.mark.asyncio
async def test_wait_uses_run_snapshot_when_lifecycle_delivery_is_lost():
    gateway = InProcessSubagentGateway(inbox_capacity=1)
    root_id = gateway.ensure_root("session-result-snapshot")
    release = asyncio.Event()

    async def runner(_run_id: str) -> str:
        await release.wait()
        return "verdict: FAIL\nfindings: snapshot"

    child = await gateway.spawn(
        session_id="session-result-snapshot",
        parent_run_id=root_id,
        agent_name="voidx",
        description="snapshot",
        runner=runner,
        mode="review",
    )
    await gateway.send(
        sender_run_id=child.run_id,
        target_run_id=root_id,
        message_type="message",
        payload={"text": "fill"},
    )
    release.set()

    waited = await gateway.wait(
        requester_run_id=root_id,
        target_run_id=child.run_id,
        timeout=1,
    )

    assert waited.result["mode"] == "review"
    assert waited.result["status"] == "completed"
    assert waited.result["output"] == waited.result["result"]
