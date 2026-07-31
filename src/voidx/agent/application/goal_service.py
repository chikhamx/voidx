"""Application service for autonomous Goal lifecycle."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime

from voidx.agent.domain.goal import GOAL_PROFILE, GoalSpec, GoalState
from voidx.agent.domain.thread import AgentThread, AgentThreadState, LifecycleState, RuntimeDecision
from voidx.memory.service import ensure_session
from voidx.memory.thread_store import ThreadStateConflict, ThreadStore


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


class GoalService:
    def __init__(self, *, store: ThreadStore, scheduler, workspace: str) -> None:
        self._store = store
        self._scheduler = scheduler
        self._workspace = workspace
        self._active_specs: dict[str, GoalSpec] = {}
        self._parent_locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, parent: str) -> asyncio.Lock:
        return self._parent_locks.setdefault(parent, asyncio.Lock())

    async def start(self, parent_thread_id: str | None, spec: GoalSpec) -> GoalStatus:
        parent = _parent_id(parent_thread_id)
        async with self._lock_for(parent):
            return await self._start_unlocked(parent, spec)

    async def _start_unlocked(self, parent: str, spec: GoalSpec) -> GoalStatus:
        spec = spec.model_copy(update={"generation": _new_generation()})
        await self._deactivate_current(parent, "Goal superseded by a new /goal start.")
        await self._store.discard_pending_outbox_prefix(f"goal:{parent}:")
        session_id = spec.goal_session_id(parent)
        await ensure_session(session_id, self._workspace, profile="goal")
        goal_state = GoalState.from_spec(spec, run_id=spec.generation)
        await self._store.create_thread(
            AgentThread(
                thread_id=spec.goal_thread_id(parent),
                session_id=session_id,
                parent_thread_id=parent,
                workspace=self._workspace,
                lifecycle=LifecycleState.READY,
            ),
            profile=GOAL_PROFILE,
            state=AgentThreadState(
                thread_id=spec.goal_thread_id(parent),
                lifecycle=LifecycleState.READY,
                context={
                    "goal_spec": spec.model_dump(mode="json"),
                    "goal_run": goal_state.model_dump(mode="json"),
                },
            ),
        )
        self._active_specs[parent] = spec
        register = getattr(self._scheduler, "register_goal_thread", None)
        if callable(register):
            register(spec.goal_thread_id(parent))
        await self._scheduler.run_goal(parent, spec)
        start_pump = getattr(self._scheduler, "start_pump", None)
        if callable(start_pump):
            start_pump()
        status = await self._status(parent, include_terminal=True)
        if status is None:
            raise RuntimeError("goal failed to start")
        return status

    async def status(self, parent_thread_id: str | None) -> GoalStatus | None:
        return await self._status(_parent_id(parent_thread_id), include_terminal=False)

    async def _status(self, parent: str, *, include_terminal: bool) -> GoalStatus | None:
        spec = await self._active_spec(parent)
        if spec is None:
            return None
        loaded = await self._store.load(spec.goal_thread_id(parent))
        terminal = {
            LifecycleState.COMPLETED,
            LifecycleState.FAILED,
            LifecycleState.CANCELLED,
        }
        if loaded is None:
            return None
        if loaded.state.lifecycle in terminal:
            if not include_terminal:
                self._active_specs.pop(parent, None)
                return None
            self._active_specs.pop(parent, None)
        state = GoalState.model_validate(loaded.state.context["goal_run"])
        return GoalStatus(
            active=loaded.state.lifecycle not in terminal,
            parent_thread_id=parent,
            goal_thread_id=loaded.thread.thread_id,
            objective_summary=spec.objective_summary(),
            acceptance_summary=spec.acceptance_condition.replace("\n", " ")[:80],
            attempt_count=state.attempt_count,
            max_attempts=state.max_attempts,
            last_evaluator_summary=state.last_evaluator_summary,
            state=loaded.state.lifecycle.value,
        )

    async def stop(self, parent_thread_id: str | None) -> bool:
        parent = _parent_id(parent_thread_id)
        async with self._lock_for(parent):
            return await self._deactivate_current(parent, "Goal stopped by user.")

    async def _deactivate_current(self, parent: str, summary: str) -> bool:
        spec = await self._active_spec(parent)
        if spec is None:
            return False
        self._active_specs.pop(parent, None)
        thread_id = spec.goal_thread_id(parent)
        unregister = getattr(self._scheduler, "unregister_goal_thread", None)
        if callable(unregister):
            unregister(thread_id)
        for _ in range(2):
            loaded = await self._store.load(thread_id)
            if loaded is None or loaded.state.lifecycle in {
                LifecycleState.COMPLETED,
                LifecycleState.FAILED,
                LifecycleState.CANCELLED,
            }:
                break
            stopped = loaded.state.model_copy(
                update={
                    "lifecycle": LifecycleState.CANCELLED,
                    "lifecycle_decision": RuntimeDecision(
                        outcome="stop", summary=summary, progress="partial"
                    ),
                }
            )
            try:
                await self._store.save_state(
                    thread_id, stopped, expected_state_version=loaded.state_version
                )
                break
            except ThreadStateConflict:
                continue
        await self._store.discard_pending_outbox(thread_id)
        return True

    async def _active_spec(self, parent: str) -> GoalSpec | None:
        spec = self._active_specs.get(parent)
        if spec is not None:
            return spec
        return await self._restore_active_spec(parent)

    async def _restore_active_spec(self, parent: str) -> GoalSpec | None:
        thread_id = await self._store.latest_thread_id_with_prefix(f"goal:{parent}:")
        if thread_id is None:
            return None
        loaded = await self._store.load(thread_id)
        if loaded is None or loaded.profile.profile_id != "goal":
            return None
        if loaded.state.lifecycle in {
            LifecycleState.COMPLETED,
            LifecycleState.FAILED,
            LifecycleState.CANCELLED,
        }:
            return None
        raw_spec = loaded.state.context.get("goal_spec")
        if not isinstance(raw_spec, dict):
            return None
        spec = GoalSpec.model_validate(raw_spec)
        self._active_specs[parent] = spec
        register = getattr(self._scheduler, "register_goal_thread", None)
        if callable(register):
            register(thread_id)
        start_pump = getattr(self._scheduler, "start_pump", None)
        if callable(start_pump):
            start_pump()
        return spec


def _parent_id(parent_thread_id: str | None) -> str:
    return (parent_thread_id or "default").strip() or "default"


def _new_generation() -> str:
    return f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}-{uuid.uuid4().hex[:8]}"
