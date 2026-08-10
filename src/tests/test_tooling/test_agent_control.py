"""Tests for child-agent run control."""

from __future__ import annotations

import asyncio
import time

import pytest
from pydantic import ValidationError

from voidx.agent.adapters.tools.context import AgentToolExecutionContext as ToolContext, AgentToolRuntime
import voidx.agent.adapters.tools.subagent_control as control_module
from voidx.agent.adapters.tools.subagent_control import AgentControlInput, AgentControlTool, _WAIT_TIMEOUT
from voidx.agent.domain.subagent import AgentGatewayError, AgentRun, AgentToolActivity
from voidx.agent.domain.subagent_display import subagent_display_name
from voidx.tooling.domain.schema import model_to_json_schema


def _run(
    run_id: str,
    *,
    status: str = "running",
    result: dict | None = None,
    error: str | None = None,
    wait_outcome: str | None = None,
    active_tools: list[AgentToolActivity] | None = None,
    last_tool: AgentToolActivity | None = None,
) -> AgentRun:
    return AgentRun(
        run_id=run_id,
        session_id="session-control",
        parent_run_id="root",
        agent_type="sub",
        agent_name="voidx",
        description=run_id,
        status=status,
        result=result,
        error=error,
        created_at=1.0,
        updated_at=2.0,
        active_tools=active_tools or [],
        last_tool=last_tool,
        wait_outcome=wait_outcome,
    )


class FakeTransport:
    def __init__(self, runs: dict[str, AgentRun | Exception]):
        self.runs = runs
        self.calls: list[tuple[str, str, float | None]] = []
        self.active = 0
        self.max_active = 0

    async def wait(self, *, requester_run_id: str, target_run_id: str, timeout: float):
        self.calls.append(("wait", target_run_id, timeout))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        value = self.runs[target_run_id]
        if isinstance(value, Exception):
            raise value
        return value

    async def cancel(self, *, requester_run_id: str, target_run_id: str):
        self.calls.append(("cancel", target_run_id, None))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        value = self.runs[target_run_id]
        if isinstance(value, Exception):
            raise value
        return value


def _ctx(transport=None) -> ToolContext:
    return ToolContext(
        workspace=".",
        session_id="session-control",
        runtime=AgentToolRuntime(subagent_transport=transport, run_id="root"),
    )


def test_agent_control_schema_and_timeout_mapping():
    schema = model_to_json_schema(AgentControlInput)
    assert set(schema["properties"]) == {"action", "run_id"}
    assert set(schema["properties"]["action"]["enum"]) == {"wait", "cancel"}
    assert schema["properties"]["run_id"]["type"] == ["string", "array"]
    assert _WAIT_TIMEOUT == 256.0


def test_agent_control_normalizes_and_deduplicates_run_ids():
    assert AgentControlInput(action="wait", run_id=" run_a ").run_id == ["run_a"]
    assert AgentControlInput(
        action="cancel",
        run_id=[" run_a ", "run_b", "run_a"],
    ).run_id == ["run_a", "run_b"]

    for run_id in (" ", [], ["run_a", " "]):
        with pytest.raises(ValidationError):
            AgentControlInput(action="wait", run_id=run_id)


@pytest.mark.asyncio
async def test_wait_single_completed_uses_compact_output_and_compatible_metadata():
    run = _run(
        "run_done",
        status="completed",
        result={"result": "verdict: PASS"},
        wait_outcome="terminal_reached_during_wait",
    )
    result = await AgentControlTool().execute(
        {"action": "wait", "run_id": run.run_id},
        _ctx(FakeTransport({run.run_id: run})),
    )

    name = subagent_display_name(run.run_id)
    assert result.output == f"{name} [completed]\nResult:\nverdict: PASS"
    assert result.display == f"{name} completed."
    assert result.summary == f"{name} completed"
    assert result.next_step_hint == ""
    assert set(result.metadata) == {
        "run", "status", "wait_outcome", "terminal", "result_quality", "finish_reason"
    }


@pytest.mark.asyncio
async def test_wait_timeout_uses_fixed_256s_and_short_hint(monkeypatch):
    monkeypatch.setattr(control_module, "_WAIT_TIMEOUT", 0.01)
    monkeypatch.setattr(time, "time", lambda: 11.0)
    run = _run("run_slow", wait_outcome="timed_out_still_running")
    transport = FakeTransport({run.run_id: run})

    result = await AgentControlTool().execute(
        {"action": "wait", "run_id": run.run_id},
        _ctx(transport),
    )

    assert result.output == (
        f"{subagent_display_name(run.run_id)} [running]\nStatus: elapsed 10s"
    )
    assert transport.calls == [("wait", run.run_id, 0.01)]
    assert result.next_step_hint == (
        "The child agent is still running and may need more time; wait again later if the result is still needed."
    )


@pytest.mark.asyncio
async def test_wait_timeout_reports_elapsed_and_active_tool(monkeypatch):
    monkeypatch.setattr(control_module, "_WAIT_TIMEOUT", 0.01)
    monkeypatch.setattr(time, "time", lambda: 11.0)
    run = _run(
        "run_active",
        wait_outcome="timed_out_still_running",
        active_tools=[AgentToolActivity(
            tool_name="search",
            tool_call_id="call-search",
            status="running",
            started_at=8.0,
        )],
    )

    result = await AgentControlTool().execute(
        {"action": "wait", "run_id": run.run_id},
        _ctx(FakeTransport({run.run_id: run})),
    )

    assert result.output == (
        f"{subagent_display_name(run.run_id)} [running]\n"
        "Status: elapsed 10s · active: search (3s)"
    )


@pytest.mark.asyncio
async def test_wait_failed_and_incomplete_results_emit_recovery_hints():
    failed = _run("run_failed", status="failed", error="provider failed", wait_outcome="already_terminal")
    incomplete = _run(
        "run_incomplete",
        status="completed",
        result={"result": "partial", "finish_reason": "contract_unsatisfied"},
        wait_outcome="already_terminal",
    )

    failed_result = await AgentControlTool().execute(
        {"action": "wait", "run_id": failed.run_id},
        _ctx(FakeTransport({failed.run_id: failed})),
    )
    incomplete_result = await AgentControlTool().execute(
        {"action": "wait", "run_id": incomplete.run_id},
        _ctx(FakeTransport({incomplete.run_id: incomplete})),
    )

    assert failed_result.output.endswith("[failed]\nError: provider failed")
    assert failed_result.next_step_hint == (
        "Inspect the error and start a replacement run if the task is still needed."
    )
    assert "[completed; finish_reason=contract_unsatisfied]" in incomplete_result.output
    assert incomplete_result.next_step_hint == (
        "Use the partial result if sufficient; otherwise start a narrower replacement task."
    )


@pytest.mark.asyncio
async def test_batch_wait_is_concurrent_ordered_and_reports_partial_error():
    first = _run("run_first", status="completed", result={"result": "one"}, wait_outcome="already_terminal")
    third = _run("run_third", wait_outcome="timed_out_still_running")
    denied = AgentGatewayError("Route not allowed", reason="route_not_allowed")
    transport = FakeTransport({first.run_id: first, "run_denied": denied, third.run_id: third})

    result = await AgentControlTool().execute(
        {
            "action": "wait",
            "run_id": [first.run_id, "run_denied", third.run_id],
        },
        _ctx(transport),
    )

    headings = [line for line in result.output.splitlines() if line and "[" in line]
    assert headings == [
        f"{subagent_display_name(first.run_id)} [completed]",
        f"{subagent_display_name('run_denied')} [error]",
        f"{subagent_display_name(third.run_id)} [running]",
    ]
    assert transport.max_active == 3
    assert result.display == "3 agents"
    assert result.metadata["partial_error"] is True
    assert "error" not in result.metadata
    assert result.metadata["counts"] == {"completed": 1, "error": 1, "running": 1}
    assert [item["run_id"] for item in result.metadata["items"]] == [
        first.run_id, "run_denied", third.run_id
    ]
    assert result.next_step_hint == "\n".join([
        "The child agent is still running and may need more time; wait again later if the result is still needed.",
        "Verify the run IDs and parent-child control relationship before retrying.",
    ])


@pytest.mark.asyncio
async def test_batch_all_control_errors_sets_top_level_error_and_deduplicates_guidance():
    transport = FakeTransport({
        "run_a": AgentGatewayError("Unknown run", reason="unknown_run"),
        "run_b": AgentGatewayError("Route not allowed", reason="route_not_allowed"),
    })

    result = await AgentControlTool().execute(
        {"action": "cancel", "run_id": ["run_a", "run_b"]},
        _ctx(transport),
    )

    assert result.metadata["error"] is True
    assert "partial_error" not in result.metadata
    assert result.next_step_hint == (
        "Verify the run IDs and parent-child control relationship before retrying."
    )


@pytest.mark.asyncio
async def test_cancel_success_and_timeout_metadata_and_hints():
    cancelled = _run("run_cancelled", status="cancelled")
    timeout = AgentGatewayError(
        "Child cancellation was not acknowledged",
        reason="cancel_timeout",
    )

    success = await AgentControlTool().execute(
        {"action": "cancel", "run_id": cancelled.run_id},
        _ctx(FakeTransport({cancelled.run_id: cancelled})),
    )
    failed = await AgentControlTool().execute(
        {"action": "cancel", "run_id": "run_timeout"},
        _ctx(FakeTransport({"run_timeout": timeout})),
    )

    assert success.output == f"{subagent_display_name(cancelled.run_id)} [cancelled]"
    assert set(success.metadata) == {"run", "status"}
    assert success.next_step_hint == ""
    assert failed.metadata == {
        "error": True,
        "reason": "cancel_timeout",
        "detail": "Child cancellation was not acknowledged",
        "run_id": "run_timeout",
    }
    assert failed.next_step_hint == (
        "Cancellation was not acknowledged; do not retry automatically, and report that the run may still be active."
    )


@pytest.mark.asyncio
async def test_invalid_input_and_gateway_unavailable_have_exact_recovery_hints():
    invalid = await AgentControlTool().execute(
        {"action": "wait", "run_id": []},
        _ctx(FakeTransport({})),
    )
    unavailable = await AgentControlTool().execute(
        {"action": "wait", "run_id": "run_a"},
        _ctx(None),
    )

    assert invalid.metadata["error"] is True
    assert invalid.next_step_hint == "Correct the arguments before retrying."
    assert unavailable.metadata == {"error": True, "reason": "gateway_unavailable"}
    assert unavailable.next_step_hint == (
        "Restore agent gateway availability before retrying."
    )
