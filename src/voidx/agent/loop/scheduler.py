"""Runtime-backed execution adapter for scheduled /loop prompts."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from voidx.agent.domain.loop import (
    LOOP_ITERATION_USER_TEXT,
    LoopSpec,
    LoopToolView,
    loop_profile_for_spec,
)
from voidx.agent.domain.thread import AgentThread, AgentThreadState, LifecycleState, RuntimeDecision
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.runtime.contracts import TurnRequest
from voidx.agent.runtime.dispatcher import DispatchResult, RuntimeDispatcher
from voidx.memory.thread_store import ThreadStore
from voidx.runtime.ui import StatusFinished, StatusUpdated, ui_events

LOOP_WAITING_STATUS_ID = "loop:waiting"


def _publish_loop_waiting(decision: RuntimeDecision) -> None:
    """Surface the next-wakeup time so idle UIs can show a countdown."""
    import time

    if decision.outcome == "continue" and decision.next_delay_seconds:
        ui_events.emit_nowait(StatusUpdated(
            status_id=LOOP_WAITING_STATUS_ID,
            label="Looping",
            detail=str(time.time() + float(decision.next_delay_seconds)),
            display="record_only",
        ))
    else:
        ui_events.emit_nowait(StatusFinished(status_id=LOOP_WAITING_STATUS_ID))

_DEFAULT_DYNAMIC_DELAY_SECONDS = 600.0
from voidx.agent.loop.controller import LoopAttemptController





@dataclass(frozen=True)
class LoopRuntimeRunner:
    runtime: object

    async def run_turn(self, *, thread, profile, input_frame: dict) -> RuntimeDecision:
        prompt = str(input_frame.get("prompt", ""))
        if not prompt.strip():
            return RuntimeDecision(
                outcome="failed",
                summary="Loop prompt was empty.",
                reason="empty_loop_prompt",
            )
        spec = LoopSpec.model_validate(input_frame.get("spec") or {"prompt": prompt})
        controller = LoopAttemptController(spec=spec)
        ui_events.emit_nowait(StatusFinished(status_id=LOOP_WAITING_STATUS_ID))
        runtime_profile = loop_profile_for_spec(spec)
        context = TurnExecutionContext(
            thread_id=thread.thread_id,
            session_id=thread.session_id or "",
            runtime_profile=runtime_profile,
            workspace=thread.workspace,
            tool_policy=LoopToolView.default(workflow_enabled=False).bind(_available_loop_tool_ids()),
            loop_controller=controller,
        )
        await self.runtime.run_turn(
            TurnRequest(
                thread=thread,
                user_text=LOOP_ITERATION_USER_TEXT,
                display_text=str(input_frame.get("display_text") or "") or None,
                context=context,
                runtime=None,
                persist_user_input=False,
            )
        )
        submitted = controller.final_decision()
        if submitted is not None:
            _publish_loop_waiting(submitted)
            return submitted
        fallback = await controller.submit_decision(
            RuntimeDecision(
                outcome="continue",
                summary="Iteration ended without a loop decision; continuing with the default delay.",
                next_delay_seconds=None if spec.interval_seconds is not None else _DEFAULT_DYNAMIC_DELAY_SECONDS,
                reason="no_loop_decision_submitted",
            )
        )
        _publish_loop_waiting(fallback)
        return fallback


class LoopRuntimeScheduler:
    def __init__(
        self,
        *,
        store: ThreadStore,
        runtime,
        workspace: str,
        lease_owner: str = "loop-manager",
        lease_seconds: float = 60,
        pump_poll_seconds: float = 1.0,
        session_id: str = "",
    ) -> None:
        self._store = store
        self._runtime = runtime
        self._workspace = workspace
        self._lease_owner = lease_owner
        self._lease_seconds = lease_seconds
        self._pump_poll_seconds = pump_poll_seconds
        self._session_id = session_id
        # Loop threads this session owns (started or explicitly resumed).
        # The pump only claims wakeups for these — a global outbox row for any
        # other session is never this session's job to run.
        self._managed_thread_ids: set[str] = set()
        self._pump_task: asyncio.Task | None = None

    def register_loop_thread(self, thread_id: str) -> None:
        if thread_id:
            self._managed_thread_ids.add(thread_id)

    def unregister_loop_thread(self, thread_id: str) -> None:
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

    async def _dispatch_next_wakeup(self):
        """Claim and run the next due wakeup owned by this session."""
        if not self._managed_thread_ids:
            return None
        dispatcher = RuntimeDispatcher(
            store=self._store,
            runner=LoopRuntimeRunner(self._runtime),
            lease_owner=self._lease_owner,
            lease_seconds=self._lease_seconds,
            claim_kind="wakeup",
        )
        for _ in range(16):
            outbox = await self._store.claim_next_outbox(
                lease_owner=self._lease_owner,
                lease_seconds=self._lease_seconds,
                kind="wakeup",
            )
            if outbox is None:
                return None
            if outbox.thread_id not in self._managed_thread_ids:
                # Not ours: release the claim and leave it pending for the
                # session that owns (or resumes) this loop.
                await self._store.release_outbox_claim(outbox.outbox_id)
                continue
            return await dispatcher._dispatch_claimed(outbox)
        return None

    async def _pump_loop(self) -> None:
        while True:
            try:
                result = await self._dispatch_next_wakeup()
            except Exception:
                logging.getLogger(__name__).exception("loop wakeup pump dispatch failed")
                result = None
            if result is None:
                await asyncio.sleep(self._pump_poll_seconds)

    async def run_prompt(
        self,
        prompt: str,
        *,
        display_text: str | None,
        session_id: str | None,
        spec: LoopSpec | None = None,
    ) -> DispatchResult | None:
        loop_spec = spec or LoopSpec(prompt=prompt)
        loaded = await self._ensure_thread(session_id, loop_spec)
        outbox = await self._store.enqueue_outbox(
            thread_id=loaded.thread.thread_id,
            kind="loop_prompt",
            payload={
                "prompt": prompt,
                "display_text": display_text,
                "spec": loop_spec.model_dump(mode="json"),
            },
            expected_state_version=loaded.state_version,
        )
        dispatcher = RuntimeDispatcher(
            store=self._store,
            runner=LoopRuntimeRunner(self._runtime),
            lease_owner=self._lease_owner,
            lease_seconds=self._lease_seconds,
        )
        return await dispatcher.dispatch_outbox(outbox.outbox_id)

    async def _ensure_thread(self, session_id: str | None, spec: LoopSpec):
        thread_id = spec.loop_thread_id(session_id)
        loaded = await self._store.load(thread_id)
        if loaded is not None:
            return loaded
        return await self._store.create_thread(
            AgentThread(
                thread_id=thread_id,
                session_id=spec.loop_session_id(session_id),
                parent_thread_id=session_id,
                workspace=self._workspace,
            ),
            profile=loop_profile_for_spec(spec),
            state=AgentThreadState(thread_id=thread_id, lifecycle=LifecycleState.READY),
            resource_scope={"workspace": self._workspace},
        )


def _available_loop_tool_ids() -> set[str]:
    return {
        "loop",
        "read",
        "find",
        "search",
        "lsp",
        "document",
        "websearch",
        "webfetch",
        "mcp",
        "skill",
        "bash",
        "workflow",
        "task_status",
        "todo",
    }
