"""Session runtime use cases."""

from voidx.agent.domain.state import AgentRuntime
from voidx.agent.ports.session import SessionStore


class SessionService:
    def __init__(self, store: SessionStore) -> None:
        self._store = store

    async def restore_runtime(self, session_id: str) -> AgentRuntime:
        return await self._store.load_runtime(session_id)

    async def persist_runtime(self, session_id: str, runtime: AgentRuntime) -> None:
        await self._store.save_runtime(session_id, runtime)

    async def clear_runtime(self, session_id: str) -> None:
        await self._store.clear_runtime(session_id)
