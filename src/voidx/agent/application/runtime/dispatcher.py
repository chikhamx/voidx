"""Durable outbox dispatcher for runtime-backed autonomous turns."""

from __future__ import annotations

import asyncio

from dataclasses import dataclass
from typing import Protocol

from voidx.agent.domain.agent_profile import ResolvedAgentProfile
from voidx.agent.domain.thread import RuntimeDecision
from voidx.agent.application.runtime.lifecycle import LifecycleController
from voidx.agent.ports.persistence import ThreadStateConflict, ThreadStore
from voidx.agent.ports.presentation import AgentEventPublisher


class RuntimeTurnRunner(Protocol):
    async def run_turn(self, *, thread, profile: ResolvedAgentProfile, input_frame: dict) -> RuntimeDecision: ...


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
        events: AgentEventPublisher | None = None,
    ) -> None:
        self._store = store
        self._runner = runner
        self._lease_owner = lease_owner
        self._lease_seconds = lease_seconds
        self._lifecycle = lifecycle or LifecycleController()
        self._claim_kind = claim_kind
        self._events = events

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
            await self._store.ack_outbox(outbox.outbox_id)
            return None
        if attempt.status == "committed":
            await self._store.ack_outbox(outbox.outbox_id)
            return None
        if attempt.side_effect_started:
            await self._store.release_outbox_claim(outbox.outbox_id)
            return None
        attempt = await self._store.mark_side_effect_started(
            attempt.attempt_id,
            lease_owner=self._lease_owner,
            fencing_token=attempt.fencing_token,
        )
        if attempt is None:
            await self._store.release_outbox_claim(outbox.outbox_id)
            return None
        lease_lost = asyncio.Event()

        async def renew_lease() -> None:
            interval = max(0.005, self._lease_seconds / 3)
            while not lease_lost.is_set():
                try:
                    await asyncio.wait_for(lease_lost.wait(), timeout=interval)
                    return
                except asyncio.TimeoutError:
                    try:
                        renewed = await self._store.renew_attempt_lease(
                            attempt.attempt_id,
                            lease_owner=self._lease_owner,
                            fencing_token=attempt.fencing_token,
                            lease_seconds=self._lease_seconds,
                        )
                    except Exception:
                        renewed = False
                    if not renewed:
                        lease_lost.set()
                        return

        renewal_task = asyncio.create_task(renew_lease())
        try:
            try:
                decision = await self._runner.run_turn(
                    thread=loaded.thread,
                    profile=loaded.resolved_profile,
                    input_frame=input_frame,
                )
            except Exception as exc:
                try:
                    await self._store.set_needs_user_for_attempt(
                        attempt.attempt_id,
                        reason=f"Runtime turn failed after side effect started: {exc}",
                        lease_owner=self._lease_owner,
                        fencing_token=attempt.fencing_token,
                    )
                except ThreadStateConflict:
                    await self._store.release_outbox_claim(outbox.outbox_id)
                    return None
                except Exception:
                    await self._store.release_outbox_claim(outbox.outbox_id)
                    raise
                await self._store.ack_outbox(outbox.outbox_id)
                return None
            if lease_lost.is_set():
                try:
                    await self._store.set_needs_user_for_attempt(
                        attempt.attempt_id,
                        reason="Runtime turn lease was lost before commit.",
                        lease_owner=self._lease_owner,
                        fencing_token=attempt.fencing_token,
                    )
                except ThreadStateConflict:
                    await self._store.release_outbox_claim(outbox.outbox_id)
                    return None
                except Exception:
                    await self._store.release_outbox_claim(outbox.outbox_id)
                    raise
                await self._store.ack_outbox(outbox.outbox_id)
                return None
            decision = self._lifecycle.normalize_decision(decision)
            try:
                await self._store.commit_decision(
                    attempt_id=attempt.attempt_id,
                    decision=decision,
                    expected_state_version=attempt.state_version,
                    lease_owner=self._lease_owner,
                    fencing_token=attempt.fencing_token,
                )
            except (ThreadStateConflict, ValueError):
                await self._store.ack_outbox(outbox.outbox_id)
                return None
            await self._store.ack_outbox(outbox.outbox_id)
            if decision.outcome == "needs_user" and self._events is not None:
                reason = decision.reason or decision.summary
                self._events.publish_message(
                    f"Automation paused for user input: {reason}"
                )
            return DispatchResult(
                attempt_id=attempt.attempt_id,
                thread_id=outbox.thread_id,
                decision=decision,
            )
        finally:
            lease_lost.set()
            renewal_task.cancel()
            try:
                await renewal_task
            except asyncio.CancelledError:
                pass
