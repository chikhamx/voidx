"""Tests for child-agent run control."""

from voidx.agent.adapters.tools.subagent_control import AgentControlInput, AgentControlTool, _WAIT_TIMEOUTS
from voidx.tooling.domain.schema import model_to_json_schema


def test_agent_control_schema_and_timeout_mapping():
    schema = model_to_json_schema(AgentControlInput)
    assert set(schema["properties"]) == {"action", "run_id", "wait"}
    assert set(schema["properties"]["action"]["enum"]) == {"wait", "cancel"}
    assert set(schema["properties"]["wait"]["enum"]) == {"brief", "extended", "until_complete"}
    assert _WAIT_TIMEOUTS == {"brief": 5.0, "extended": 30.0, "until_complete": 0.0}


def test_agent_control_required_fields_are_explicit():
    assert set(AgentControlInput.model_json_schema()["required"]) >= {"action", "run_id"}


def test_agent_control_cancel_ignores_wait_strategy():
    inp = AgentControlInput(action="cancel", run_id="run_123", wait="extended")
    assert inp.action == "cancel"
    assert inp.run_id == "run_123"
    assert inp.wait == "extended"


import asyncio

import pytest

from voidx.agent.gateway import AgentGateway
from voidx.agent.adapters.tools.context import AgentToolExecutionContext as ToolContext, AgentToolRuntime


@pytest.mark.asyncio
async def test_agent_control_wait_exposes_timeout_while_child_is_still_running():
    gateway = AgentGateway()
    root_id = gateway.ensure_root("session-control-timeout")
    started = asyncio.Event()

    async def runner(_run_id: str) -> str:
        started.set()
        await asyncio.sleep(10)
        return "late"

    run = await gateway.spawn(
        session_id="session-control-timeout",
        parent_run_id=root_id,
        agent_name="voidx",
        description="slow child",
        runner=runner,
    )
    await started.wait()

    result = await AgentControlTool().execute(
        {"action": "wait", "run_id": run.run_id, "wait": "brief"},
        ToolContext(
            workspace=".",
            session_id="session-control-timeout",
            runtime=AgentToolRuntime(subagent_transport=gateway, run_id=root_id),
        ),
    )

    assert "Agent run status: running" in result.output
    assert "Wait outcome: timed_out_still_running" in result.output
    assert "Terminal: false" in result.output
    assert "Do not call wait repeatedly in a tight loop" in result.output
    assert result.next_step_hint == f"Run {run.run_id} is still active; do not poll in a tight loop."
    await gateway.cancel(requester_run_id=root_id, target_run_id=run.run_id)


@pytest.mark.asyncio
async def test_agent_control_wait_stops_polling_after_terminal_result():
    gateway = AgentGateway()
    root_id = gateway.ensure_root("session-control-terminal")

    async def runner(_run_id: str) -> str:
        return "verdict: PASS\nfindings: none"

    run = await gateway.spawn(
        session_id="session-control-terminal",
        parent_run_id=root_id,
        agent_name="voidx",
        description="fast child",
        runner=runner,
    )
    context = ToolContext(
        workspace=".",
        session_id="session-control-terminal",
        runtime=AgentToolRuntime(subagent_transport=gateway, run_id=root_id),
    )

    first = await AgentControlTool().execute(
        {"action": "wait", "run_id": run.run_id, "wait": "brief"},
        context,
    )
    second = await AgentControlTool().execute(
        {"action": "wait", "run_id": run.run_id, "wait": "brief"},
        context,
    )

    assert "Wait outcome: terminal_reached_during_wait" in first.output
    assert "Do not call agent_control(wait) again" in first.output
    assert "Wait outcome: already_terminal" in second.output
    assert "This wait returned the cached terminal result" in second.output
    assert "Do not call agent_control(wait) again" in second.output


@pytest.mark.asyncio
async def test_agent_control_wait_marks_contract_unsatisfied_as_terminal_incomplete_result():
    gateway = AgentGateway()
    root_id = gateway.ensure_root("session-control-contract")

    async def runner(_run_id: str) -> dict:
        return {
            "result": "findings: tests passed, but verdict is missing",
            "finish_reason": "contract_unsatisfied",
        }

    run = await gateway.spawn(
        session_id="session-control-contract",
        parent_run_id=root_id,
        agent_name="voidx",
        description="incomplete review",
        runner=runner,
    )
    result = await AgentControlTool().execute(
        {"action": "wait", "run_id": run.run_id, "wait": "brief"},
        ToolContext(
            workspace=".",
            session_id="session-control-contract",
            runtime=AgentToolRuntime(subagent_transport=gateway, run_id=root_id),
        ),
    )

    assert "Result quality: incomplete_contract" in result.output
    assert "The child run is terminal and cannot produce a new result by waiting" in result.output
    assert "Do not call agent_control(wait) again" in result.output
