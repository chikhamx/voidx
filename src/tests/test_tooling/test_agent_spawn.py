"""Tests for agent spawn output structure and summary."""

from __future__ import annotations

import pytest

from voidx.agent.adapters.tools.context import AgentToolExecutionContext as ToolContext, AgentToolRuntime
from voidx.agent.adapters.tools.subagent import AgentTool
from voidx.agent.domain.subagent import AgentRun
from voidx.agent.domain.subagent_display import subagent_display_name


class FakeSpawnGateway:
    def __init__(self, run: AgentRun):
        self._run = run

    async def spawn(self, **kwargs):
        return self._run


@pytest.mark.asyncio
async def test_agent_spawn_output_structure_and_summary(tmp_path):
    run_id = "run_8bf0d23519a843dd9213989e25427944"
    display_name = subagent_display_name(run_id)
    run = AgentRun(
        run_id=run_id,
        session_id="spawn-session",
        parent_run_id="root",
        agent_type="sub",
        agent_name="voidx",
        description="Review spawn result",
        status="running",
        created_at=1.0,
        updated_at=1.0,
    )

    async def runner(*args, **kwargs):
        return "ok"

    tool = AgentTool(
        runner,
        agent_resolver=lambda name: type("Agent", (), {"name": name})(),
    )
    result = await tool.execute(
        {
            "mode": "review",
            "goal": "Review spawn result",
            "detail": "A sufficiently complete execution brief.",
            "scope": "",
        },
        ToolContext(
            workspace=str(tmp_path),
            session_id="spawn-session",
            runtime=AgentToolRuntime(subagent_transport=FakeSpawnGateway(run), run_id="root"),
        ),
    )

    expected_output = (
        f"name: {display_name}\n"
        f"status: running\n"
        f"run_id: {run_id}"
    )
    assert result.output == expected_output
    assert result.summary == "spawned"
    assert f"run_id='{run_id}'" in result.next_step_hint
