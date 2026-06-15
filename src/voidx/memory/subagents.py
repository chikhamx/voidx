"""Per-session subagent transcript persistence."""

from __future__ import annotations

from typing import Any

from voidx.memory.jsonl_store import append_session_record
from voidx.memory.store import _now


async def append_subagent_event(
    session_id: str,
    agent_run_id: str,
    event: dict[str, Any],
) -> None:
    record = {
        **event,
        "agent_run_id": agent_run_id,
        "created_at": event.get("created_at") or _now(),
    }
    await append_session_record(session_id, f"subagents/{agent_run_id}.jsonl", record)
