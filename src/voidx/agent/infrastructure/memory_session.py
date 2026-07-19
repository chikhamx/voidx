"""Adapter from session application port to the existing memory service."""

from voidx.agent.domain.state import AgentRuntime
from voidx.agent.infrastructure.runtime_state_mapper import (
    agent_runtime_from_snapshot,
    snapshot_from_agent_runtime,
)
from voidx.memory.service import clear_runtime_state, load_runtime_state, save_runtime_state


class MemorySessionAdapter:
    async def load_runtime(self, session_id: str) -> AgentRuntime:
        return agent_runtime_from_snapshot(await load_runtime_state(session_id))

    async def save_runtime(self, session_id: str, runtime: AgentRuntime) -> None:
        await save_runtime_state(session_id, snapshot_from_agent_runtime(runtime))

    async def clear_runtime(self, session_id: str) -> None:
        await clear_runtime_state(session_id)
