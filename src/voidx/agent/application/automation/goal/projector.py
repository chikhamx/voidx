"""Atomic projection entry point for durable Goal protocol records."""

from __future__ import annotations

from voidx.agent.domain.automation.goal import GoalProtocolRecord
from voidx.agent.ports.persistence import ThreadStore


class GoalProjector:
    """Project one submitted Goal record without executing a runtime phase."""

    def __init__(self, *, store: ThreadStore) -> None:
        self._store = store

    async def project(self, protocol_id: str) -> GoalProtocolRecord:
        record = await self._store.get_goal_protocol(protocol_id)
        if record is None:
            raise KeyError(protocol_id)
        if record.status == "projected":
            return record
        return await self._store.project_goal_protocol(protocol_id)
