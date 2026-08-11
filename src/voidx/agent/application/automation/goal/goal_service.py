"""Application service for autonomous Goal lifecycle."""

from __future__ import annotations

from dataclasses import dataclass

from voidx.agent.application.autonomous import (
    AutonomousServiceBase,
    GoalScheduler,
    new_generation,
    parent_id,
)
from voidx.agent.domain.automation.goal import GOAL_PROFILE, GoalSpec, GoalState
from voidx.agent.ports.subagent import ParentResultPublisher
from voidx.agent.domain.thread import (
    TERMINAL_LIFECYCLES,
    AgentThread,
    AgentThreadState,
    LifecycleState,
)
from voidx.agent.ports.persistence import ThreadStore


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


class GoalService(AutonomousServiceBase[GoalSpec, GoalScheduler]):
    def __init__(
        self,
        *,
        store: ThreadStore,
        scheduler: GoalScheduler,
        workspace: str,
        result_publisher: ParentResultPublisher | None = None,
    ) -> None:
        super().__init__(store=store, scheduler=scheduler, workspace=workspace)
        self._result_publisher = result_publisher
        self._terminal_notified: set[str] = set()

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
        spec = spec.model_copy(update={"generation": new_generation()})
        await self._deactivate_current(parent, summary="Goal superseded by a new /goal start.")
        await self._store.discard_pending_outbox_prefix(f"goal:{parent}:")
        session_id = spec.goal_session_id(parent)
        await self._store.ensure_session(
            session_id,
            self._workspace,
            profile="goal",
            root_session_id=parent,
        )
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
        self._register_thread(spec.goal_thread_id(parent))
        await self._scheduler.run_goal(parent, spec)
        self._start_pump()
        status = await self._status(parent, include_terminal=True)
        if status is None:
            raise RuntimeError("goal failed to start")
        return status

    async def status(self, parent_thread_id: str | None) -> GoalStatus | None:
        return await self._status(parent_id(parent_thread_id), include_terminal=False)

    async def _status(self, parent: str, *, include_terminal: bool) -> GoalStatus | None:
        spec = await self._active_spec(parent)
        if spec is None:
            return None
        loaded = await self._store.load(spec.goal_thread_id(parent))
        if loaded is None:
            return None
        if loaded.state.lifecycle in TERMINAL_LIFECYCLES:
            if not include_terminal:
                return None
            self._active_specs.pop(parent, None)
            self._notify_terminal_once(parent, spec, loaded)
        state = GoalState.model_validate(loaded.state.context["goal_run"])
        return GoalStatus(
            active=loaded.state.lifecycle not in TERMINAL_LIFECYCLES,
            parent_thread_id=parent,
            goal_thread_id=loaded.thread.thread_id,
            objective_summary=spec.objective_summary(),
            acceptance_summary=spec.acceptance_condition.replace("\n", " ")[:80],
            attempt_count=state.attempt_count,
            max_attempts=state.max_attempts,
            last_evaluator_summary=state.last_evaluator_summary,
            state=loaded.state.lifecycle.value,
        )

    def _notify_terminal_once(self, parent: str, spec: GoalSpec, loaded) -> None:
        if self._result_publisher is None:
            return
        goal_thread_id = spec.goal_thread_id(parent)
        if goal_thread_id in self._terminal_notified:
            return
        self._terminal_notified.add(goal_thread_id)
        state = GoalState.model_validate(loaded.state.context["goal_run"])
        summary = state.last_evaluator_summary or state.blocked_reason or ""
        text = (
            f"[goal finished] objective: {spec.objective_summary()}\n"
            f"outcome: {loaded.state.lifecycle.value} "
            f"(attempts {state.attempt_count}/{state.max_attempts})"
            + (f"\nsummary: {summary}" if summary else "")
        )
        self._result_publisher.publish(parent, text)

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
        if loaded.state.lifecycle in TERMINAL_LIFECYCLES:
            return None
        raw_spec = loaded.state.context.get("goal_spec")
        if not isinstance(raw_spec, dict):
            return None
        spec = GoalSpec.model_validate(raw_spec)
        self._active_specs[parent] = spec
        self._register_thread(thread_id)
        self._start_pump()
        return spec
