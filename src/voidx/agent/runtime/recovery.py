"""Recovery helpers for durable runtime turn attempts."""

from __future__ import annotations

from dataclasses import dataclass

from voidx.memory.thread_store import ThreadStore


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
            await self._store.set_needs_user_for_attempt(
                attempt_id,
                reason="Attempt recovery stopped because side effect started before commit.",
            )
            return RecoveryResult(attempt_id=attempt_id, action="needs_user")
        return RecoveryResult(attempt_id=attempt_id, action="retry")
