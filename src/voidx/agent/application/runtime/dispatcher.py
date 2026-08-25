"""Durable outbox dispatcher for runtime-backed autonomous turns."""

from __future__ import annotations

import asyncio

from dataclasses import dataclass
from typing import Any, Protocol

from voidx.agent.application.runtime.contracts import GoalPhaseResult
from voidx.agent.application.runtime.lifecycle import LifecycleController
from voidx.agent.domain.agent_profile import ResolvedAgentProfile
from voidx.agent.domain.automation.goal import GoalRuntimeFailure
from voidx.agent.domain.thread import RuntimeDecision
from voidx.agent.ports.persistence import (
    GoalProtocolConflict,
    GoalRuntimeCorruption,
    ThreadStateConflict,
    ThreadStore,
)
from voidx.agent.ports.presentation import AgentEventPublisher


class RuntimeTurnRunner(Protocol):
    async def run_turn(
        self,
        *,
        thread,
        profile: ResolvedAgentProfile,
        input_frame: dict,
    ) -> RuntimeDecision | GoalPhaseResult: ...


@dataclass(frozen=True)
class DispatchResult:
    attempt_id: str
    thread_id: str
    decision: RuntimeDecision | None = None
    goal_phase: GoalPhaseResult | None = None


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
        guidance: Any | None = None,
    ) -> None:
        self._store = store
        self._runner = runner
        self._lease_owner = lease_owner
        self._lease_seconds = lease_seconds
        self._lifecycle = lifecycle or LifecycleController()
        self._claim_kind = claim_kind
        self._events = events
        self._guidance = guidance

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
        guidance_delivery_id = f"attempt:{outbox.outbox_id}"
        guidance_bound = False
        if self._guidance is not None:
            bound_guidance = await self._guidance.bind_delivery(
                guidance_delivery_id,
                session_id=loaded.thread.session_id or "",
                thread_id=outbox.thread_id,
                run_id=_guidance_run_id(outbox),
                phase=_guidance_phase(outbox),
            )
            if bound_guidance:
                input_frame["guidance"] = [
                    _guidance_snapshot(guidance) for guidance in bound_guidance
                ]
                guidance_bound = True

        async def release_guidance() -> None:
            nonlocal guidance_bound
            if guidance_bound and self._guidance is not None:
                await self._guidance.release_delivery(guidance_delivery_id)
                guidance_bound = False

        async def commit_guidance() -> None:
            nonlocal guidance_bound
            if guidance_bound and self._guidance is not None:
                await self._guidance.commit_delivery(guidance_delivery_id)
                guidance_bound = False

        try:
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
                await release_guidance()
                await self._store.ack_outbox(outbox.outbox_id)
                return None

            if attempt.status == "committed":
                await release_guidance()
                await self._store.ack_outbox(outbox.outbox_id)
                return None
            if attempt.side_effect_started:
                await release_guidance()
                await self._store.release_outbox_claim(outbox.outbox_id)
                return None

            attempt = await self._store.mark_side_effect_started(
                attempt.attempt_id,
                lease_owner=self._lease_owner,
                fencing_token=attempt.fencing_token,
            )
            if attempt is None:
                await release_guidance()
                await self._store.release_outbox_claim(outbox.outbox_id)
                return None

            frozen_frame = await self._store.get_attempt_input_frame(attempt.attempt_id)
            runner_frame = {
                **frozen_frame,
                "attempt_id": attempt.attempt_id,
                "lease_owner": self._lease_owner,
                "fencing_token": attempt.fencing_token,
            }
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
                    result = await self._runner.run_turn(
                        thread=loaded.thread,
                        profile=loaded.resolved_profile,
                        input_frame=runner_frame,
                    )
                except GoalRuntimeCorruption as exc:
                    goal_state = runner_frame.get("goal_state") or {}
                    observed_sequence = (
                        exc.observed_sequence
                        if exc.observed_sequence >= 0
                        else int(goal_state.get("projected_sequence_number", -1))
                    )
                    await self._store.fail_goal_generation(
                        GoalRuntimeFailure(
                            generation=str(runner_frame.get("generation") or ""),
                            observed_sequence=observed_sequence,
                            reason=str(exc),
                            evidence=exc.evidence,
                        )
                    )
                    await self._store.deliver_goal_public_summaries(
                        generation=str(runner_frame.get("generation") or "")
                    )
                    await release_guidance()
                    return None
                except Exception as exc:
                    try:
                        await self._store.set_needs_user_for_attempt(
                            attempt.attempt_id,
                            reason=f"Runtime turn failed after side effect started: {exc}",
                            lease_owner=self._lease_owner,
                            fencing_token=attempt.fencing_token,
                        )
                    except ThreadStateConflict:
                        await release_guidance()
                        await self._store.release_outbox_claim(outbox.outbox_id)
                        return None
                    except Exception:
                        await release_guidance()
                        await self._store.release_outbox_claim(outbox.outbox_id)
                        raise
                    await release_guidance()
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
                        await release_guidance()
                        await self._store.release_outbox_claim(outbox.outbox_id)
                        return None
                    except Exception:
                        await release_guidance()
                        await self._store.release_outbox_claim(outbox.outbox_id)
                        raise
                    await release_guidance()
                    await self._store.ack_outbox(outbox.outbox_id)
                    return None

                if isinstance(result, GoalPhaseResult):
                    try:
                        if result.protocol_id:
                            await self._store.commit_goal_phase(
                                attempt_id=attempt.attempt_id,
                                protocol_id=result.protocol_id,
                                lease_owner=self._lease_owner,
                                fencing_token=attempt.fencing_token,
                                guidance_delivery_id=(
                                    guidance_delivery_id if guidance_bound else ""
                                ),
                            )
                        else:
                            await self._store.commit_goal_needs_resume(
                                attempt_id=attempt.attempt_id,
                                phase=result.phase,
                                reason=result.reason or "missing_goal_protocol",
                                lease_owner=self._lease_owner,
                                fencing_token=attempt.fencing_token,
                                guidance_delivery_id=(
                                    guidance_delivery_id if guidance_bound else ""
                                ),
                            )
                        guidance_bound = False
                        await self._store.deliver_goal_public_summaries(
                            generation=str(runner_frame.get("generation") or "")
                        )
                    except (GoalProtocolConflict, ThreadStateConflict, ValueError, KeyError):
                        await release_guidance()
                        await self._store.release_outbox_claim(outbox.outbox_id)
                        return None
                    return DispatchResult(
                        attempt_id=attempt.attempt_id,
                        thread_id=outbox.thread_id,
                        goal_phase=result,
                    )

                decision = self._lifecycle.normalize_decision(result)
                try:
                    await self._store.commit_decision(
                        attempt_id=attempt.attempt_id,
                        decision=decision,
                        expected_state_version=attempt.state_version,
                        lease_owner=self._lease_owner,
                        fencing_token=attempt.fencing_token,
                    )
                except (ThreadStateConflict, ValueError):
                    await release_guidance()
                    await self._store.ack_outbox(outbox.outbox_id)
                    return None

                await commit_guidance()
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
        finally:
            await release_guidance()


def _guidance_run_id(outbox: Any) -> str:
    payload = getattr(outbox, "payload", {}) or {}
    goal_state = payload.get("goal_state") or {}
    return str(payload.get("run_id") or goal_state.get("run_id") or "")


def _guidance_phase(outbox: Any) -> str:
    payload = getattr(outbox, "payload", {}) or {}
    goal_state = payload.get("goal_state") or {}
    return str(
        payload.get("phase")
        or payload.get("goal_phase")
        or goal_state.get("current_phase")
        or "work"
    )


def _guidance_snapshot(guidance: Any) -> dict[str, Any]:
    return {
        "guidance_id": guidance.guidance_id,
        "text": guidance.text,
        "source": guidance.source,
        "truncated": guidance.truncated,
        "target_phase": guidance.target_phase,
    }
