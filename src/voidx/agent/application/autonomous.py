"""Shared plumbing for autonomous goal/loop services.

Holds the parent-scoped lock/state bookkeeping, thread-id helpers, and the
cancel-with-CAS lifecycle that GoalService and LoopService share, plus the
scheduler protocols they depend on.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
import uuid
from datetime import datetime
from typing import Generic, Protocol, TypeVar

from voidx.agent.domain.agent_profile import ResolvedAgentProfile
from voidx.agent.domain.automation.goal import GoalSpec
from voidx.agent.domain.automation.loop import LoopSpec
from voidx.agent.domain.thread import TERMINAL_LIFECYCLES, LifecycleState, RuntimeDecision
from voidx.agent.application.agent_profile_snapshot import restore_session_profile
from voidx.agent.application.agent_registry import AgentRegistry
from voidx.agent.application.runtime.dispatcher import DispatchResult
from voidx.agent.ports.persistence import ThreadStateConflict, ThreadStore

SpecT = TypeVar("SpecT")
SchedulerT = TypeVar("SchedulerT")


class GoalScheduler(Protocol):
    def register_goal_thread(self, thread_id: str) -> None: ...
    def unregister_goal_thread(self, thread_id: str) -> None: ...
    def start_pump(self) -> None: ...
    async def stop_pump(self) -> None: ...
    async def run_goal(self, parent_thread_id: str | None, spec: GoalSpec) -> DispatchResult | None: ...


class LoopScheduler(Protocol):
    def register_loop_thread(self, thread_id: str) -> None: ...
    def unregister_loop_thread(self, thread_id: str) -> None: ...
    def start_pump(self) -> None: ...
    async def stop_pump(self) -> None: ...
    async def run_prompt(
        self,
        prompt: str,
        *,
        display_text: str | None,
        session_id: str | None,
        spec: LoopSpec | None = None,
    ) -> DispatchResult | None: ...


class AutonomousServiceBase(ABC, Generic[SpecT, SchedulerT]):
    """Parent-scoped lifecycle bookkeeping shared by goal and loop services."""

    def __init__(self, *, store: ThreadStore, scheduler: SchedulerT, workspace: str) -> None:
        self._store = store
        self._scheduler = scheduler
        self._workspace = workspace
        self._active_specs: dict[str, SpecT] = {}
        self._parent_locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, parent: str) -> asyncio.Lock:
        return self._parent_locks.setdefault(parent, asyncio.Lock())

    async def _resolve_attempt_profile(self, parent: str, legacy_id: str) -> ResolvedAgentProfile:
        """Resolve the profile to pin for a new goal/loop attempt.

        Precedence: the parent session's pinned snapshot, then the parent
        session's profile id via the registry, then the bundled preset for the
        legacy id. An unresolvable parent profile surfaces as-is — never
        silently downgrade to a different profile.
        """
        registry = AgentRegistry(self._workspace)
        parent_thread = await self._store.load(parent)
        session_id = parent_thread.thread.session_id if parent_thread is not None else ""
        info = await self._store.get_session(session_id) if session_id else None
        if info is not None:
            return restore_session_profile(
                registry,
                profile_id=info.runtime_profile,
                snapshot=info.profile_snapshot,
            )
        return registry.resolve(legacy_id)

    @abstractmethod
    def _spec_thread_id(self, spec: SpecT, parent: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def _register_thread(self, thread_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def _unregister_thread(self, thread_id: str) -> None:
        raise NotImplementedError

    def _on_deactivated(self) -> None:
        """Hook for profile-specific UI cleanup after a cancel."""

    async def _active_spec(self, parent: str) -> SpecT | None:
        return self._active_specs.get(parent)

    async def _deactivate_current(self, parent: str, *, summary: str) -> bool:
        spec = await self._active_spec(parent)
        if spec is None:
            return False
        self._active_specs.pop(parent, None)
        thread_id = self._spec_thread_id(spec, parent)
        self._unregister_thread(thread_id)
        self._on_deactivated()
        for _ in range(2):
            loaded = await self._store.load(thread_id)
            if loaded is None or loaded.state.lifecycle in TERMINAL_LIFECYCLES:
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

    def _start_pump(self) -> None:
        self._scheduler.start_pump()

    async def _stop_pump(self) -> None:
        await self._scheduler.stop_pump()


def parent_id(parent_thread_id: str | None) -> str:
    return (parent_thread_id or "default").strip() or "default"


def new_generation() -> str:
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}-{uuid.uuid4().hex[:8]}"
