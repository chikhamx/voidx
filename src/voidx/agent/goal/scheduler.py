"""Durable scheduler for autonomous Goal runtime turns."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from voidx.agent.domain.goal import GoalSpec, GoalState
from voidx.agent.goal.runner import GoalRuntimeRunner
from voidx.agent.runtime.dispatcher import DispatchResult, RuntimeDispatcher
from voidx.memory.thread_store import ThreadStore


class GoalEvaluatorFactory(Protocol):
    def __call__(self): ...


class GoalRuntimeScheduler:
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
    ) -> None:
        self._store = store
        self._runtime = runtime
        self._workspace = workspace
        self._evaluator = evaluator
        self._lease_owner = lease_owner
        self._lease_seconds = lease_seconds
        self._pump_poll_seconds = pump_poll_seconds
        self._managed_thread_ids: set[str] = set()
        self._pump_task: asyncio.Task | None = None

    def register_goal_thread(self, thread_id: str) -> None:
        if thread_id:
            self._managed_thread_ids.add(thread_id)

    def unregister_goal_thread(self, thread_id: str) -> None:
        self._managed_thread_ids.discard(thread_id)

    def start_pump(self) -> None:
        if self._pump_task is not None:
            return
        self._pump_task = asyncio.create_task(self._pump_loop())

    async def stop_pump(self) -> None:
        task, self._pump_task = self._pump_task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def run_goal(self, parent_thread_id: str | None, spec: GoalSpec) -> DispatchResult | None:
        loaded = await self._store.load(spec.goal_thread_id(parent_thread_id))
        if loaded is None:
            raise RuntimeError("goal thread must be created before scheduling")
        state = GoalState.model_validate(loaded.state.context["goal_run"])
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
        )
        return await dispatcher.dispatch_outbox(outbox.outbox_id)

    async def _dispatch_next_wakeup(self) -> DispatchResult | None:
        dispatcher = RuntimeDispatcher(
            store=self._store,
            runner=self._runner(),
            lease_owner=self._lease_owner,
            lease_seconds=self._lease_seconds,
            claim_kind="wakeup",
        )
        skipped: set[str] = set()
        for _ in range(16):
            outbox = await self._store.claim_next_outbox(
                lease_owner=self._lease_owner,
                lease_seconds=self._lease_seconds,
                kind="wakeup",
                thread_id_prefix="goal:",
                exclude_outbox_ids=skipped,
            )
            if outbox is None:
                return None
            if not await self._owns_goal_wakeup(outbox.thread_id):
                skipped.add(outbox.outbox_id)
                await self._store.release_outbox_claim(outbox.outbox_id)
                continue
            self._managed_thread_ids.add(outbox.thread_id)
            return await dispatcher._dispatch_claimed(outbox)
        return None

    async def _owns_goal_wakeup(self, thread_id: str) -> bool:
        if self._managed_thread_ids:
            return thread_id in self._managed_thread_ids
        if not thread_id.startswith("goal:"):
            return False
        loaded = await self._store.load(thread_id)
        return loaded is not None and loaded.profile.profile_id == "goal"

    async def _pump_loop(self) -> None:
        while True:
            try:
                await self._dispatch_next_wakeup()
            except Exception:
                logging.getLogger(__name__).exception("goal wakeup pump dispatch failed")
            await asyncio.sleep(self._pump_poll_seconds)

    def _runner(self):
        if self._evaluator is None:
            return self._runtime
        return GoalRuntimeRunner(runtime=self._runtime, evaluator=self._evaluator)
