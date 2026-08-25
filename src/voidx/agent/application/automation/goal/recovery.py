"""Linear recovery for durable Goal protocol generations."""

from __future__ import annotations

from uuid import uuid4

from voidx.agent.application.automation.goal.projector import GoalProjector
from voidx.agent.domain.automation.goal import (
    GoalProtocolRecord,
    GoalRuntimeFailure,
    GoalState,
    goal_sequence_number,
    is_goal_terminal,
)
from voidx.agent.ports.persistence import (
    GoalProtocolConflict,
    GoalRuntimeCorruption,
    ThreadStore,
)


class GoalRecovery:
    """Replay submitted Goal records and ensure the next phase wakeup."""

    def __init__(self, *, store: ThreadStore, lease_seconds: float = 30.0) -> None:
        self._store = store
        self._lease_seconds = lease_seconds
        self._projector = GoalProjector(store=store)

    async def recover_generation(
        self,
        generation: str,
        *,
        lease_owner: str | None = None,
        lease_acquired: bool = False,
    ) -> None:
        generation = generation.strip()
        if not generation:
            raise ValueError("generation must not be empty")
        owner = lease_owner or f"goal-recovery-{uuid4().hex}"
        acquired_here = False
        if not lease_acquired:
            if not await self._store.acquire_goal_generation_lease(
                generation,
                owner,
                lease_seconds=self._lease_seconds,
            ):
                raise GoalProtocolConflict("Goal generation recovery lease is held")
            acquired_here = True

        try:
            binding = await self._store.get_goal_generation(generation)
            if binding is None:
                raise KeyError(generation)
            if not binding.goal_thread_id:
                raise GoalProtocolConflict("Goal generation has no goal thread")

            while True:
                if not await self._store.renew_goal_generation_lease(
                    generation,
                    owner,
                    lease_seconds=self._lease_seconds,
                ):
                    raise GoalProtocolConflict("Goal generation recovery lease expired")
                loaded = await self._store.load(binding.goal_thread_id)
                if loaded is None:
                    raise GoalProtocolConflict("Goal thread disappeared during recovery")
                state = GoalState.model_validate(loaded.state.context.get("goal_run") or {})
                records = await self._store.list_goal_protocols(generation)
                by_sequence = self._validate_records(
                    records,
                    generation=generation,
                    binding=binding,
                    state=state,
                )
                next_record = by_sequence.get(state.projected_sequence_number + 1)
                if next_record is None:
                    break
                if next_record.status != "submitted":
                    raise GoalRuntimeCorruption(
                        "Goal journal is ahead of GoalState",
                        observed_sequence=state.projected_sequence_number,
                    )
                await self._projector.project(next_record.protocol_id)

            loaded = await self._store.load(binding.goal_thread_id)
            if loaded is None:
                raise GoalProtocolConflict("Goal thread disappeared during recovery")
            state = GoalState.model_validate(loaded.state.context.get("goal_run") or {})
            self._validate_records(
                await self._store.list_goal_protocols(generation),
                generation=generation,
                binding=binding,
                state=state,
            )
            if not is_goal_terminal(loaded.state.lifecycle):
                await self._store.ensure_goal_phase_outbox(generation)
        except GoalRuntimeCorruption as exc:
            await self._store.fail_goal_generation(
                GoalRuntimeFailure(
                    generation=generation,
                    observed_sequence=exc.observed_sequence,
                    reason=str(exc),
                    evidence=exc.evidence,
                )
            )
        finally:
            if acquired_here:
                await self._store.release_goal_generation_lease(generation, owner)

    @staticmethod
    def _validate_records(
        records: list[GoalProtocolRecord],
        *,
        generation: str,
        binding,
        state: GoalState,
    ) -> dict[int, GoalProtocolRecord]:
        expected_sessions = {
            "init": binding.main_session_id,
            "checkpoint": binding.work_session_id,
            "decision": binding.evaluator_session_id,
        }
        by_sequence: dict[int, GoalProtocolRecord] = {}
        for record in records:
            if record.generation != generation:
                raise GoalRuntimeCorruption(
                    "Goal protocol generation mismatch",
                    observed_sequence=state.projected_sequence_number,
                )
            try:
                expected_sequence = goal_sequence_number(
                    record.phase, record.attempt_number
                )
            except ValueError as exc:
                raise GoalRuntimeCorruption(
                    str(exc),
                    observed_sequence=state.projected_sequence_number,
                ) from exc
            if record.sequence_number != expected_sequence:
                raise GoalRuntimeCorruption(
                    "Goal protocol sequence mismatch",
                    observed_sequence=state.projected_sequence_number,
                    evidence=(
                        f"record={record.protocol_id}",
                        f"expected={expected_sequence}",
                        f"actual={record.sequence_number}",
                    ),
                )
            if record.parent_session_id != binding.main_session_id:
                raise GoalRuntimeCorruption(
                    "Goal protocol parent session mismatch",
                    observed_sequence=state.projected_sequence_number,
                )
            if record.session_id != expected_sessions[record.phase]:
                raise GoalRuntimeCorruption(
                    "Goal protocol phase session mismatch",
                    observed_sequence=state.projected_sequence_number,
                )
            if record.sequence_number in by_sequence:
                raise GoalRuntimeCorruption(
                    "Goal protocol sequence conflict",
                    observed_sequence=state.projected_sequence_number,
                )
            by_sequence[record.sequence_number] = record

        observed_sequence = state.projected_sequence_number
        if 0 not in by_sequence:
            raise GoalRuntimeCorruption(
                "Goal protocol sequence hole at INIT",
                observed_sequence=observed_sequence,
            )
        highest = max(by_sequence)
        missing = next(
            (sequence for sequence in range(highest + 1) if sequence not in by_sequence),
            None,
        )
        if missing is not None:
            raise GoalRuntimeCorruption(
                f"Goal protocol sequence hole at {missing}",
                observed_sequence=observed_sequence,
                evidence=(f"highest={highest}",),
            )
        if state.generation != generation:
            raise GoalRuntimeCorruption(
                "Goal state generation mismatch",
                observed_sequence=observed_sequence,
            )
        if observed_sequence < 0 or observed_sequence > highest:
            raise GoalRuntimeCorruption(
                "GoalState projected sequence is invalid",
                observed_sequence=observed_sequence,
                evidence=(f"highest={highest}",),
            )
        for sequence, record in by_sequence.items():
            if sequence == 0 and record.status != "projected":
                raise GoalRuntimeCorruption(
                    "INIT must already be projected",
                    observed_sequence=observed_sequence,
                )
            if sequence <= observed_sequence and record.status != "projected":
                raise GoalRuntimeCorruption(
                    "GoalState is ahead of an unprojected record",
                    observed_sequence=observed_sequence,
                    evidence=(f"sequence={sequence}",),
                )
            if sequence > observed_sequence and record.status == "projected":
                raise GoalRuntimeCorruption(
                    "Goal journal is ahead of GoalState",
                    observed_sequence=observed_sequence,
                    evidence=(f"sequence={sequence}",),
                )
        return by_sequence
