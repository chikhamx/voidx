"""Reproduce: AgentControlTool(action='wait', run_id=...) with default wait='until_complete' blocks forever.

Hypothesis:
  - AgentControlInput has default wait='until_complete'.
  - _WAIT_TIMEOUTS maps 'until_complete' -> 0.0.
  - InProcessSubagentGateway.wait with timeout=0 does:
        if timeout == 0:
            await target.done.wait()
    which is unbounded.

So if a child agent never reaches a terminal state (no result sent, no
cancellation, runner never returns), the parent calling Wait will hang
forever, with no timeout escape at any layer.

Run:
    ./python.py scripts/repro_wait_blocking.py

Expected (if hypothesis holds):
    Exit code 124 after HARD_DEADLINE_SECS, with "still blocked" printed.
"""
from __future__ import annotations

import asyncio
import sys
import time

from voidx.agent.adapters.subagent import InProcessSubagentGateway
from voidx.agent.adapters.tools.context import AgentToolExecutionContext as ToolContext, AgentToolRuntime
from voidx.agent.adapters.tools.subagent_control import AgentControlTool


HARD_DEADLINE_SECS = 5.0  # we expect this to hit if the tool blocks forever


async def never_ending_runner(_run_id: str) -> str:
    # Simulates a child agent whose LLM loop never returns (e.g., itself
    # waiting on something that never resolves). No release, no return.
    await asyncio.Event().wait()  # wait forever
    return "unreachable"  # pragma: no cover


async def main() -> int:
    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-repro")

    spawned = await gateway.spawn(
        session_id="session-repro",
        parent_run_id=root_id,
        agent_name="kai",
        description="never-ending child",
        runner=never_ending_runner,
    )
    print(f"[repro] spawned child run_id={spawned.run_id} status={spawned.status}")

    root_ctx = ToolContext(
        workspace="/tmp",
        session_id="session-repro",
        runtime=AgentToolRuntime(subagent_transport=gateway, run_id=root_id),
    )

    # Exactly what the trace showed: AgentControlTool(action='wait', run_id='Kai')
    # with NO `wait` field -> defaults to 'until_complete' -> _WAIT_TIMEOUTS['until_complete'] == 0.0
    args = {"action": "wait", "run_id": spawned.run_id}

    print(f"[repro] calling AgentControlTool.execute with {args}")
    print(f"[repro] if buggy, this will block past {HARD_DEADLINE_SECS}s; if fixed, it returns.")

    start = time.monotonic()
    try:
        result = await asyncio.wait_for(
            AgentControlTool().execute(args, root_ctx),
            timeout=HARD_DEADLINE_SECS,
        )
        elapsed = time.monotonic() - start
        print(f"[repro] returned in {elapsed:.2f}s -> output={result.output!r}")
        print("[repro] OK: AgentControlTool.wait completed within deadline")
        return 0
    except TimeoutError:
        elapsed = time.monotonic() - start
        print(f"[repro] still blocked after {elapsed:.2f}s")
        print("[repro] CONFIRMED: AgentControlTool(action='wait', default until_complete) blocks indefinitely")
        # Force-terminate child task so the script can exit cleanly
        await gateway.cancel(requester_run_id=root_id, target_run_id=spawned.run_id)
        return 124


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))