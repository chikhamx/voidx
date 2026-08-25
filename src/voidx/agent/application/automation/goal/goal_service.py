"""Application service for autonomous Goal lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from uuid import uuid4

from voidx.agent.application.autonomous import (
    AutonomousServiceBase,
    GoalScheduler,
    new_generation,
    parent_id,
)
from voidx.agent.domain.automation.goal import (
    GOAL_PROFILE,
    GoalProtocolRecord,
    GoalSpec,
    GoalSpecSnapshot,
    GoalState,
    goal_phase_session_id,
    is_goal_terminal,
)
from voidx.agent.domain.agent_profile import AgentProfileSnapshot, ResolvedAgentProfile
from voidx.agent.application.agent_profile_snapshot import restore_from_snapshot
from voidx.agent.application.automation.goal.recovery import GoalRecovery
from voidx.agent.domain.thread import (
    AgentThreadState,
    LifecycleState,
)
from voidx.agent.ports.persistence import GoalProtocolConflict, ThreadStore
from voidx.platform.session_ids import validate_session_storage_id


@dataclass(frozen=True)
class GoalStatus:
    active: bool
    parent_thread_id: str
    goal_thread_id: str
    objective_summary: str
    acceptance_summary: str
    attempt_count: int
    max_attempts: int
    last_evaluator_summary: str
    state: str
    generation: str = ""
    current_phase: str = "work"
    phase_status: str = "running"
    interrupt_reason: str = ""


class GoalService(AutonomousServiceBase[GoalSpec, GoalScheduler]):
    def __init__(
        self,
        *,
        store: ThreadStore,
        scheduler: GoalScheduler,
        workspace: str,
    ) -> None:
        super().__init__(store=store, scheduler=scheduler, workspace=workspace)
        self._generation_locks: dict[str, asyncio.Lock] = {}

    def _spec_thread_id(self, spec: GoalSpec, parent: str) -> str:
        return spec.goal_thread_id(parent)

    def _register_thread(self, thread_id: str) -> None:
        self._scheduler.register_goal_thread(thread_id)

    def _unregister_thread(self, thread_id: str) -> None:
        self._scheduler.unregister_goal_thread(thread_id)

    async def _active_spec(self, parent: str) -> GoalSpec | None:
        spec = self._active_specs.get(parent)
        if spec is not None:
            return spec
        return await self._restore_active_spec(parent)

    async def start(self, parent_thread_id: str | None, spec: GoalSpec) -> GoalStatus:
        parent = parent_id(parent_thread_id)
        async with self._lock_for(parent):
            return await self._start_unlocked(parent, spec)

    async def _start_unlocked(self, parent: str, spec: GoalSpec) -> GoalStatus:
        if spec.generation == "active":
            spec = spec.model_copy(update={"generation": new_generation()})
        await self._deactivate_current(parent, summary="Goal superseded by a new /goal start.")
        await self._store.discard_pending_outbox_prefix(f"goal:{parent}:")

        existing_init = next(
            (
                record
                for record in await self._store.list_goal_protocols(spec.generation)
                if record.sequence_number == 0
            ),
            None,
        )
        durable_snapshot: GoalSpecSnapshot | None = None
        if existing_init is not None:
            if existing_init.phase != "init" or existing_init.attempt_number != 0:
                raise GoalProtocolConflict("Goal INIT record has an invalid position")
            durable_snapshot = GoalSpecSnapshot.model_validate(existing_init.payload)
            if durable_snapshot.generation != spec.generation:
                raise GoalProtocolConflict("Goal INIT snapshot generation mismatch")
            durable_spec = durable_snapshot.to_spec()
            if durable_spec.model_dump(mode="json") != spec.model_dump(mode="json"):
                raise GoalProtocolConflict("Goal INIT spec conflicts with start request")
            if (
                durable_snapshot.parent_thread_id
                and durable_snapshot.parent_thread_id != parent
            ):
                raise GoalProtocolConflict("Goal INIT parent thread mismatch")
            if durable_snapshot.parent_session_id != existing_init.parent_session_id:
                raise GoalProtocolConflict("Goal INIT parent session mismatch")
            spec = durable_spec

        resolved = await self._resolve_attempt_profile(parent, "goal")
        profile_snapshot = resolved.snapshot
        workspace = self._workspace
        parent_loaded = await self._store.load(parent)
        current_main_session_id = (
            parent_loaded.thread.session_id if parent_loaded is not None else None
        )
        if not current_main_session_id and await self._store.get_session(parent) is not None:
            current_main_session_id = parent

        if durable_snapshot is not None:
            main_session_id = durable_snapshot.parent_session_id
            validate_session_storage_id(main_session_id)
            if current_main_session_id and current_main_session_id != main_session_id:
                raise GoalProtocolConflict("Goal INIT main session mismatch")
            workspace = durable_snapshot.workspace or workspace
            if durable_snapshot.profile_snapshot:
                try:
                    profile_snapshot = AgentProfileSnapshot.model_validate(
                        durable_snapshot.profile_snapshot
                    )
                    resolved = restore_from_snapshot(profile_snapshot)
                except Exception as exc:
                    raise GoalProtocolConflict(
                        "Goal INIT profile snapshot cannot be restored"
                    ) from exc
        else:
            main_session_id = current_main_session_id
            if not main_session_id:
                main_session_id = _new_goal_session_id("main")
            validate_session_storage_id(main_session_id)

        if await self._store.get_session(main_session_id) is None:
            await self._store.ensure_session(
                main_session_id,
                workspace,
                profile=resolved.snapshot.profile_id,
                root_session_id=parent,
                profile_snapshot=profile_snapshot,
            )

        evaluator_session_id = goal_phase_session_id(spec.generation, "evaluator")
        work_session_id = goal_phase_session_id(spec.generation, "work")
        goal_thread_id = spec.goal_thread_id(parent)
        goal_state = GoalState.from_spec(
            spec,
            run_id=spec.generation,
            main_session_id=main_session_id,
            work_session_id=work_session_id,
            evaluator_session_id=evaluator_session_id,
        )
        thread_state = AgentThreadState(
            thread_id=goal_thread_id,
            lifecycle=LifecycleState.READY,
            context={
                "goal_spec": spec.model_dump(mode="json"),
                "goal_run": goal_state.model_dump(mode="json"),
            },
        )
        snapshot = durable_snapshot or GoalSpecSnapshot.from_spec(
            spec,
            parent_session_id=main_session_id,
            parent_thread_id=parent,
            workspace=workspace,
            profile_snapshot=profile_snapshot.model_dump(mode="json"),
        )
        protocol = existing_init or GoalProtocolRecord.submitted(
            protocol_id=f"goal-init-{uuid4().hex}",
            parent_session_id=main_session_id,
            generation=spec.generation,
            phase="init",
            attempt_number=0,
            turn_id=f"goal-init-{spec.generation}",
            session_id=main_session_id,
            payload=snapshot,
        )
        await self._store.initialize_goal_generation(
            generation=spec.generation,
            main_session_id=main_session_id,
            evaluator_session_id=evaluator_session_id,
            work_session_id=work_session_id,
            goal_thread_id=goal_thread_id,
            parent_thread_id=parent,
            workspace=self._workspace,
            profile_id=resolved.snapshot.profile_id,
            profile_snapshot=resolved.snapshot,
            thread_profile=resolved,
            thread_state=thread_state,
            protocol=protocol,
        )
        self._active_specs[parent] = spec
        self._register_thread(goal_thread_id)
        await self._scheduler.run_goal(parent, spec)
        self._start_pump()
        status = await self._status(parent, include_terminal=True)
        if status is None:
            raise RuntimeError("goal failed to start")
        return status

    async def resume_generation(self, generation: str) -> GoalStatus | None:
        """Replay one Goal generation without dispatching a new runtime turn."""
        generation = generation.strip()
        if not generation:
            raise ValueError("generation must not be empty")
        lock = self._generation_locks.setdefault(generation, asyncio.Lock())
        async with lock:
            existing_binding = await self._store.get_goal_generation(generation)
            if existing_binding is not None and existing_binding.goal_thread_id:
                existing_loaded = await self._store.load(existing_binding.goal_thread_id)
                if existing_loaded is not None and is_goal_terminal(existing_loaded.state.lifecycle):
                    raw_spec = existing_loaded.state.context.get("goal_spec")
                    if not isinstance(raw_spec, dict):
                        raise GoalProtocolConflict("Terminal Goal spec is missing")
                    terminal_spec = GoalSpec.model_validate(raw_spec)
                    if terminal_spec.generation != generation:
                        raise GoalProtocolConflict("Terminal Goal generation mismatch")
                    terminal_parent = (
                        existing_loaded.thread.parent_thread_id
                        or existing_binding.main_session_id
                    )
                    self._active_specs[terminal_parent] = terminal_spec
                    return await self._status(terminal_parent, include_terminal=True)

            lease_owner = f"goal-recovery-{uuid4().hex}"
            acquired = await self._store.acquire_goal_generation_lease(
                generation,
                lease_owner,
                lease_seconds=30.0,
            )
            if not acquired:
                raise GoalProtocolConflict("Goal generation recovery lease is held")
            try:
                binding = await self._store.get_goal_generation(generation)
                if binding is None:
                    records = await self._store.list_goal_protocols(generation)
                    init = next(
                        (record for record in records if record.sequence_number == 0),
                        None,
                    )
                    if init is None or init.phase != "init":
                        raise KeyError(generation)
                    snapshot = GoalSpecSnapshot.model_validate(init.payload)
                    if snapshot.generation != generation:
                        raise GoalProtocolConflict("INIT snapshot generation mismatch")
                    parent = snapshot.parent_thread_id.strip()
                    if not parent:
                        raise GoalProtocolConflict("INIT snapshot parent thread is missing")
                    main_session_id = snapshot.parent_session_id
                    validate_session_storage_id(main_session_id)
                    spec = snapshot.to_spec()
                    resolved = await self._resolve_attempt_profile(parent, "goal")
                    profile_snapshot = (
                        AgentProfileSnapshot.model_validate(snapshot.profile_snapshot)
                        if snapshot.profile_snapshot
                        else resolved.snapshot
                    )
                    if profile_snapshot.profile_id != resolved.snapshot.profile_id:
                        raise GoalProtocolConflict("Goal profile snapshot binding conflict")
                    work_session_id = goal_phase_session_id(generation, "work")
                    evaluator_session_id = goal_phase_session_id(generation, "evaluator")
                    goal_thread_id = spec.goal_thread_id(parent)
                    goal_state = GoalState.from_spec(
                        spec,
                        run_id=generation,
                        main_session_id=main_session_id,
                        work_session_id=work_session_id,
                        evaluator_session_id=evaluator_session_id,
                    )
                    thread_state = AgentThreadState(
                        thread_id=goal_thread_id,
                        lifecycle=LifecycleState.READY,
                        context={
                            "goal_spec": spec.model_dump(mode="json"),
                            "goal_run": goal_state.model_dump(mode="json"),
                        },
                    )
                    if init.parent_session_id != main_session_id:
                        raise GoalProtocolConflict("INIT parent session mismatch")
                    await self._store.initialize_goal_generation(
                        generation=generation,
                        main_session_id=main_session_id,
                        evaluator_session_id=evaluator_session_id,
                        work_session_id=work_session_id,
                        goal_thread_id=goal_thread_id,
                        parent_thread_id=parent,
                        workspace=snapshot.workspace or self._workspace,
                        profile_id=profile_snapshot.profile_id,
                        profile_snapshot=profile_snapshot,
                        thread_profile=resolved,
                        thread_state=thread_state,
                        protocol=init,
                    )
                    binding = await self._store.get_goal_generation(generation)
                if binding is None:
                    raise GoalProtocolConflict("Goal Boundary I did not create a binding")
                if not binding.goal_thread_id:
                    raise GoalProtocolConflict("Goal generation has no goal thread")

                loaded = await self._store.load(binding.goal_thread_id)
                if loaded is None:
                    raise GoalProtocolConflict("Goal thread is missing")
                raw_spec = loaded.state.context.get("goal_spec")
                if not isinstance(raw_spec, dict):
                    raise GoalProtocolConflict("Goal spec is missing from Goal thread")
                spec = GoalSpec.model_validate(raw_spec)
                if spec.generation != generation:
                    raise GoalProtocolConflict("Goal spec generation mismatch")
                parent = loaded.thread.parent_thread_id or binding.main_session_id
                recovery = GoalRecovery(store=self._store)
                await recovery.recover_generation(
                    generation,
                    lease_owner=lease_owner,
                    lease_acquired=True,
                )
                loaded = await self._store.load(binding.goal_thread_id)
                if loaded is None:
                    raise GoalProtocolConflict("Goal thread disappeared during recovery")

                await self._store.deliver_goal_public_summaries(generation=generation)
                was_active = parent in self._active_specs
                self._active_specs[parent] = spec
                if not is_goal_terminal(loaded.state.lifecycle) and not was_active:
                    self._register_thread(binding.goal_thread_id)
                    self._start_pump()
                return await self._status(parent, include_terminal=True)
            finally:
                await self._store.release_goal_generation_lease(generation, lease_owner)


    async def status(self, parent_thread_id: str | None) -> GoalStatus | None:
        return await self._status(parent_id(parent_thread_id), include_terminal=False)

    async def _status(self, parent: str, *, include_terminal: bool) -> GoalStatus | None:
        spec = await self._active_spec(parent)
        if spec is None:
            return None
        loaded = await self._store.load(spec.goal_thread_id(parent))
        if loaded is None:
            return None
        if is_goal_terminal(loaded.state.lifecycle):
            if not include_terminal:
                return None
            state = GoalState.model_validate(loaded.state.context["goal_run"])
            await self._store.deliver_goal_public_summaries(
                generation=state.generation
            )
            self._active_specs.pop(parent, None)
        state = GoalState.model_validate(loaded.state.context["goal_run"])
        return GoalStatus(
            active=not is_goal_terminal(loaded.state.lifecycle),
            parent_thread_id=parent,
            goal_thread_id=loaded.thread.thread_id,
            objective_summary=spec.objective_summary(),
            acceptance_summary=spec.acceptance_condition.replace("\n", " ")[:80],
            attempt_count=state.attempt_count,
            max_attempts=state.max_attempts,
            last_evaluator_summary=state.last_evaluator_summary,
            state=loaded.state.lifecycle.value,
            generation=state.generation,
            current_phase=state.current_phase,
            phase_status=state.phase_status,
            interrupt_reason=state.interrupt_reason,
        )


    async def stop(self, parent_thread_id: str | None) -> bool:
        parent = parent_id(parent_thread_id)
        async with self._lock_for(parent):
            return await self._deactivate_current(parent, summary="Goal stopped by user.")

    async def _restore_active_spec(self, parent: str) -> GoalSpec | None:
        thread_id = await self._store.latest_thread_id_with_prefix(f"goal:{parent}:")
        if thread_id is None:
            return None
        loaded = await self._store.load(thread_id)
        if loaded is None or loaded.profile.profile_id != "goal":
            return None
        if is_goal_terminal(loaded.state.lifecycle):
            return None
        raw_spec = loaded.state.context.get("goal_spec")
        if not isinstance(raw_spec, dict):
            return None
        spec = GoalSpec.model_validate(raw_spec)
        self._active_specs[parent] = spec
        self._register_thread(thread_id)
        self._start_pump()
        return spec


def _new_goal_session_id(prefix: str) -> str:
    session_id = f"{prefix}-{uuid4().hex}"
    return validate_session_storage_id(session_id)
