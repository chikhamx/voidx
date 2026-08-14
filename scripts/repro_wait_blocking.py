"""Verify that the default AgentControlTool wait is finite."""
from __future__ import annotations

import asyncio
import sys
import time

from voidx.agent.adapters.subagent import InProcessSubagentGateway
from voidx.agent.adapters.tools.context import AgentToolExecutionContext as ToolContext, AgentToolRuntime
import voidx.agent.adapters.tools.subagent_control as control_module
from voidx.agent.adapters.tools.subagent_control import AgentControlTool


TEST_TIMEOUT_SECS = 0.05
HARD_DEADLINE_SECS = 1.0


async def never_ending_runner(_run_id: str) -> str:
    await asyncio.Event().wait()
    return "unreachable"


async def main() -> int:
    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-repro")
    spawned = await gateway.spawn(
        session_id="session-repro",
        parent_run_id=root_id,
        agent_name="voidx",
        description="never-ending child",
        runner=never_ending_runner,
    )
    root_ctx = ToolContext(
        workspace="/tmp",
        session_id="session-repro",
        runtime=AgentToolRuntime(subagent_transport=gateway, run_id=root_id),
    )
    control_module._WAIT_TIMEOUT = TEST_TIMEOUT_SECS

    start = time.monotonic()
    try:
        result = await asyncio.wait_for(
            AgentControlTool().execute(
                {"action": "wait", "run_id": spawned.run_id},
                root_ctx,
            ),
            timeout=HARD_DEADLINE_SECS,
        )
    except TimeoutError:
        print("[repro] FAIL: default wait exceeded the hard deadline")
        return 1
    finally:
        await gateway.cancel(requester_run_id=root_id, target_run_id=spawned.run_id)

    elapsed = time.monotonic() - start
    if result.metadata.get("wait_outcome") != "timed_out":
        print(f"[repro] FAIL: unexpected metadata {result.metadata!r}")
        return 1
    if result.metadata.get("status") != "running":
        print(f"[repro] FAIL: expected running status, got {result.metadata!r}")
        return 1
    if "terminal" in result.metadata:
        print(f"[repro] FAIL: redundant terminal metadata leaked {result.metadata!r}")
        return 1
    public_run = result.metadata.get("run") or {}
    if "active_tools" in public_run or "last_tool" in public_run:
        print(f"[repro] FAIL: concrete tool details leaked {public_run!r}")
        return 1
    expected_wait = f"No activity was observed during the {TEST_TIMEOUT_SECS:g}s wait"
    if expected_wait not in result.next_step_hint or "Cancel the child agent" not in result.next_step_hint:
        print(f"[repro] FAIL: unexpected next step hint {result.next_step_hint!r}")
        return 1
    print(f"[repro] PASS: default wait returned after {elapsed:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
