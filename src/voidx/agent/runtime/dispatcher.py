"""Durable outbox dispatcher for runtime-backed autonomous turns."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from voidx.agent.domain.thread import RuntimeDecision
from voidx.agent.runtime.lifecycle import LifecycleController
from voidx.memory.thread_store import ThreadStateConflict, ThreadStore


class RuntimeTurnRunner(Protocol):
    async def run_turn(self, *, thread, profile, input_frame: dict) -> RuntimeDecision: ...


@dataclass(frozen=True)
class DispatchResult:
    attempt_id: str
    thread_id: str
    decision: RuntimeDecision


class RuntimeDispatcher:
    def __init__(
        self,
        *,
        store: ThreadStore,
        runner: RuntimeTurnRunner,
        lease_owner: str,
        lease_seconds: float = 60,
        lifecycle: LifecycleController | None = None,
        claim_kind: str | None = None,
    ) -> None:
        self._store = store
        self._runner = runner
        self._lease_owner = lease_owner
        self._lease_seconds = lease_seconds
        self._lifecycle = lifecycle or LifecycleController()
        self._claim_kind = claim_kind

    async def dispatch_once(self) -> DispatchResult | None:
        outbox = await self._store.claim_next_outbox(
            lease_owner=self._lease_owner,
            lease_seconds=self._lease_seconds,
            kind=self._claim_kind,
        )
        if outbox is None:
            return None
        return await self._dispatch_claimed(outbox)

    async def dispatch_outbox(self, outbox_id: str) -> DispatchResult | None:
        outbox = await self._store.claim_outbox(
            outbox_id,
            lease_owner=self._lease_owner,
            lease_seconds=self._lease_seconds,
        )
        if outbox is None:
            return None
        return await self._dispatch_claimed(outbox)

    async def _dispatch_claimed(self, outbox) -> DispatchResult | None:
        loaded = await self._store.load(outbox.thread_id)
        if loaded is None:
            await self._store.ack_outbox(outbox.outbox_id)
            return None
        input_frame = {"kind": outbox.kind, **outbox.payload}
        try:
            attempt = await self._store.begin_attempt(
                thread_id=outbox.thread_id,
                source_outbox_id=outbox.outbox_id,
                input_frame=input_frame,
                expected_state_version=outbox.expected_state_version,
                lease_owner=self._lease_owner,
                lease_seconds=self._lease_seconds,
            )
        except ThreadStateConflict:
            # Stale row from a superseded thread generation (stop/restart): poison, ack and skip.
            await self._store.ack_outbox(outbox.outbox_id)
            return None
        if attempt.status == "committed":
            # Redelivery after a crash between commit and ack: do not re-run the turn.
            await self._store.ack_outbox(outbox.outbox_id)
            return None
        decision = await self._runner.run_turn(
            thread=loaded.thread,
            profile=loaded.profile,
            input_frame=input_frame,
        )
        decision = self._lifecycle.normalize_decision(decision)
        try:
            await self._store.commit_decision(
                attempt_id=attempt.attempt_id,
                decision=decision,
                expected_state_version=attempt.state_version,
            )
        except ThreadStateConflict:
            # The thread moved on while the turn ran (e.g. user stop): ack, never retry.
            await self._store.ack_outbox(outbox.outbox_id)
            return None
        await self._store.ack_outbox(outbox.outbox_id)
        return DispatchResult(
            attempt_id=attempt.attempt_id,
            thread_id=outbox.thread_id,
            decision=decision,
        )
