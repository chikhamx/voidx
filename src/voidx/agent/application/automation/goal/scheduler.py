"""Durable scheduler for autonomous Goal runtime turns."""

from __future__ import annotations

from typing import Any, Protocol

from voidx.agent.domain.automation.goal import GoalSpec, GoalState
from voidx.agent.application.automation.goal.runner import GoalRuntimeRunner
from voidx.agent.application.runtime.dispatcher import DispatchResult, RuntimeDispatcher
from voidx.agent.application.runtime.pump import WakeupPumpMixin
from voidx.agent.ports.persistence import ThreadStore
from voidx.agent.ports.presentation import AgentEventPublisher, NullAgentEventPublisher


class GoalEvaluatorFactory(Protocol):
    def __call__(self): ...


class GoalRuntimeScheduler(WakeupPumpMixin):
    def __init__(
        self,
        *,
        store: ThreadStore,
        runtime,
        workspace: str,
        evaluator=None,
        lease_owner: str = "goal-manager",
        lease_seconds: float = 60,
        pump_poll_seconds: float = 1.0,
        events: AgentEventPublisher | None = None,
        guidance: Any | None = None,
    ) -> None:
        self._store = store
        self._runtime = runtime
        self._workspace = workspace
        self._evaluator = evaluator
        self._events = events or NullAgentEventPublisher()
        self._guidance = guidance
        self._init_pump(
            lease_owner=lease_owner,
            lease_seconds=lease_seconds,
            pump_poll_seconds=pump_poll_seconds,
        )

    def register_goal_thread(self, thread_id: str) -> None:
        self.register_managed_thread(thread_id)

    def unregister_goal_thread(self, thread_id: str) -> None:
        self.unregister_managed_thread(thread_id)

    async def run_goal(self, parent_thread_id: str | None, spec: GoalSpec) -> DispatchResult | None:
        loaded = await self._store.load(spec.goal_thread_id(parent_thread_id))
        if loaded is None:
            raise RuntimeError("goal thread must be created before scheduling")
        state = GoalState.model_validate(loaded.state.context["goal_run"])
        pending = await self._store.list_pending_outbox(loaded.thread.thread_id)
        outbox = next((item for item in pending if item.kind == "goal_prompt"), None)
        if outbox is None:
            binding = await self._store.get_goal_generation(state.generation)
            if binding is not None:
                raise RuntimeError("bound Goal generation is missing its phase outbox")
            outbox = await self._store.enqueue_outbox(
                thread_id=loaded.thread.thread_id,
                kind="goal_prompt",
                payload={
                    "spec": spec.model_dump(mode="json"),
                    "goal_state": state.model_dump(mode="json"),
                },
                expected_state_version=loaded.state_version,
            )
        dispatcher = RuntimeDispatcher(
            store=self._store,
            runner=self._runner(),
            lease_owner=self._lease_owner,
            lease_seconds=self._lease_seconds,
            events=self._events,
            guidance=self._guidance,
        )
        return await dispatcher.dispatch_outbox(outbox.outbox_id)

    def _pump_outbox_kinds(self) -> tuple[str, ...]:
        return ("goal_prompt", "wakeup")

    def _claim_wakeup_filters(self) -> dict:
        return {"thread_id_prefix": "goal:"}

    async def _owns_wakeup(self, thread_id: str) -> bool:
        if self._managed_thread_ids:
            return thread_id in self._managed_thread_ids
        if not thread_id.startswith("goal:"):
            return False
        loaded = await self._store.load(thread_id)
        return loaded is not None and loaded.profile.protocol == "goal"

    def _on_wakeup_owned(self, outbox) -> None:
        self._managed_thread_ids.add(outbox.thread_id)

    def _runner(self):
        if self._evaluator is None:
            return self._runtime
        return GoalRuntimeRunner(
            runtime=self._runtime,
            evaluator=self._evaluator,
            store=self._store,
        )
