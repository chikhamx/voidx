"""Recovery helpers for durable runtime turn attempts."""

from __future__ import annotations

from dataclasses import dataclass

from voidx.agent.domain.thread import TERMINAL_LIFECYCLES
from voidx.agent.ports.persistence import ThreadStateConflict, ThreadStore


@dataclass(frozen=True)
class RecoveryResult:
    attempt_id: str
    action: str


class RuntimeRecoveryWorker:
    def __init__(self, *, store: ThreadStore) -> None:
        self._store = store

    async def recover_attempt(self, attempt_id: str) -> RecoveryResult:
        attempt = await self._store.get_attempt(attempt_id)
        if attempt is None:
            raise KeyError(attempt_id)
        if attempt.status == "committed":
            await self._store.ack_attempt_source_outbox(attempt_id)
            return RecoveryResult(attempt_id=attempt_id, action="committed")
        if attempt.side_effect_started:
            loaded = await self._store.load(attempt.thread_id)
            if loaded is not None and loaded.state.lifecycle in TERMINAL_LIFECYCLES:
                await self._store.ack_attempt_source_outbox(attempt_id)
                return RecoveryResult(attempt_id=attempt_id, action="terminal")
            try:
                await self._store.set_needs_user_for_attempt(
                    attempt_id,
                    reason="Attempt recovery stopped because side effect started before commit.",
                    lease_owner=attempt.lease_owner,
                    fencing_token=attempt.fencing_token,
                )
            except ThreadStateConflict:
                return RecoveryResult(attempt_id=attempt_id, action="lease_conflict")
            await self._store.ack_attempt_source_outbox(attempt_id)
            return RecoveryResult(attempt_id=attempt_id, action="needs_user")
        return RecoveryResult(attempt_id=attempt_id, action="retry")
