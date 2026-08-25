"""Shared wakeup-pump lifecycle for autonomous goal/loop schedulers."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from voidx.agent.application.runtime.dispatcher import DispatchResult, RuntimeDispatcher


class WakeupPumpMixin:
    """Polls the outbox for due wakeups and dispatches them with a runner.

    Subclasses implement ``_runner``, ``_claim_wakeup_filters``, ``_owns_wakeup``
    and optionally ``_pump_has_work`` / ``_on_wakeup_owned``.
    """

    def _init_pump(
        self,
        *,
        lease_owner: str,
        lease_seconds: float,
        pump_poll_seconds: float,
    ) -> None:
        self._lease_owner = lease_owner
        self._lease_seconds = lease_seconds
        self._pump_poll_seconds = pump_poll_seconds
        self._managed_thread_ids: set[str] = set()
        self._pump_task: asyncio.Task | None = None

    def register_managed_thread(self, thread_id: str) -> None:
        if thread_id:
            self._managed_thread_ids.add(thread_id)

    def unregister_managed_thread(self, thread_id: str) -> None:
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

    def _runner(self):
        raise NotImplementedError

    def _claim_wakeup_filters(self) -> dict[str, Any]:
        return {}

    async def _owns_wakeup(self, thread_id: str) -> bool:
        raise NotImplementedError

    def _on_wakeup_owned(self, outbox) -> None:
        return None

    def _pump_has_work(self) -> bool:
        return True

    async def _dispatch_next_wakeup(self) -> DispatchResult | None:
        """Claim and run the next due wakeup owned by this scheduler."""
        dispatcher = RuntimeDispatcher(
            store=self._store,
            runner=self._runner(),
            lease_owner=self._lease_owner,
            lease_seconds=self._lease_seconds,
            claim_kind="wakeup",
            events=getattr(self, "_events", None),
            guidance=getattr(self, "_guidance", None),
        )
        skipped: set[str] = set()
        for _ in range(16):
            outbox = await self._store.claim_next_outbox(
                lease_owner=self._lease_owner,
                lease_seconds=self._lease_seconds,
                kind="wakeup",
                exclude_outbox_ids=skipped,
                **self._claim_wakeup_filters(),
            )
            if outbox is None:
                return None
            if not await self._owns_wakeup(outbox.thread_id):
                # Not ours: release the claim and leave it pending for the
                # session that owns (or resumes) this autonomous thread.
                skipped.add(outbox.outbox_id)
                await self._store.release_outbox_claim(outbox.outbox_id)
                continue
            self._on_wakeup_owned(outbox)
            return await dispatcher._dispatch_claimed(outbox)
        return None

    async def _pump_loop(self) -> None:
        while True:
            try:
                if not self._pump_has_work():
                    result = None
                else:
                    result = await self._dispatch_next_wakeup()
            except Exception:
                logging.getLogger(__name__).exception("wakeup pump dispatch failed")
                result = None
            if result is None:
                await asyncio.sleep(self._pump_poll_seconds)
