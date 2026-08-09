"""Reproduce: does MessageTool(action='receive', timeout=N) actually respect the timeout?

Question:
  In the original trace the user showed, `Messageing receive(..., timeout=120)`
  appeared stuck. We want to know whether MessageTool.receive itself hangs past
  timeout, OR whether it returns cleanly at the deadline.

Hypothesis:
  - MessageTool.execute() calls gateway.receive(run_id, limit, timeout).
  - gateway.receive -> _get_one uses asyncio.wait_for(record.inbox.get(), timeout).
  - TimeoutError is caught and returns ToolResult(error=...).
  - Therefore the tool SHOULD return within `timeout` seconds even if the inbox
    is empty forever.

This script isolates the message receive deadline. Agent control waiting is
verified separately by `scripts/repro_wait_blocking.py`.

Run:
    ./python.py scripts/repro_message_receive_blocking.py
"""
from __future__ import annotations

import asyncio
import sys
import time

from voidx.agent.adapters.subagent import InProcessSubagentGateway
from voidx.agent.adapters.tools.context import AgentToolExecutionContext as ToolContext, AgentToolRuntime
from voidx.agent.adapters.tools.subagent_message import MessageTool


HARD_DEADLINE_SECS = 4.0   # outer ceiling, generous
RECEIVE_TIMEOUT = 2.0      # what we pass to the tool


async def never_ending_runner(_run_id: str) -> str:
    await asyncio.Event().wait()
    return "unreachable"  # pragma: no cover


async def main() -> int:
    gateway = InProcessSubagentGateway()
    root_id = gateway.ensure_root("session-repro-msg")

    spawned = await gateway.spawn(
        session_id="session-repro-msg",
        parent_run_id=root_id,
        agent_name="kai",
        description="never-ending",
        runner=never_ending_runner,
    )
    print(f"[repro-msg] spawned child run_id={spawned.run_id} status={spawned.status}")

    parent_ctx = ToolContext(
        workspace="/tmp",
        session_id="session-repro-msg",
        runtime=AgentToolRuntime(subagent_transport=gateway, run_id=root_id),
    )

    args = {
        "action": "receive",
        "limit": 1,
        "timeout": RECEIVE_TIMEOUT,
        "message_type": "message",
        "payload": "{}",
    }
    print(f"[repro-msg] calling MessageTool.execute({args})")
    print(f"[repro-msg] receive_timeout={RECEIVE_TIMEOUT}s, hard_deadline={HARD_DEADLINE_SECS}s")

    start = time.monotonic()
    try:
        result = await asyncio.wait_for(
            MessageTool().execute(args, parent_ctx),
            timeout=HARD_DEADLINE_SECS,
        )
        elapsed = time.monotonic() - start
        print(f"[repro-msg] returned in {elapsed:.2f}s")
        print(f"[repro-msg] output={result.output!r}")
        print(f"[repro-msg] summary={result.summary!r}")
        print(f"[repro-msg] metadata error={result.metadata.get('error')!r} "
              f"count={result.metadata.get('count')!r}")
        # If receive returned within ~RECEIVE_TIMEOUT seconds and produced
        # an empty/error result, the tool itself respects the deadline.
        if elapsed < RECEIVE_TIMEOUT + 1.5:
            print("[repro-msg] OK: Message receive respects its declared timeout.")
            ok = True
        else:
            print(f"[repro-msg] WARNING: took longer than expected ({elapsed:.2f}s > "
                  f"{RECEIVE_TIMEOUT + 1.5:.2f}s).")
            ok = False
    except TimeoutError:
        elapsed = time.monotonic() - start
        print(f"[repro-msg] HARD DEADLINE HIT after {elapsed:.2f}s")
        print("[repro-msg] CONFIRMED: Message receive blocks past its timeout.")
        ok = False
    finally:
        # Clean up the never-ending child so the process can exit.
        try:
            await asyncio.wait_for(
                gateway.cancel(requester_run_id=root_id, target_run_id=spawned.run_id),
                timeout=2.0,
            )
        except Exception:
            pass

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))