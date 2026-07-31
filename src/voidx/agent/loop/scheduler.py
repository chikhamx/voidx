"""Runtime-backed execution adapter for scheduled /loop prompts."""

from __future__ import annotations

import time
from dataclasses import dataclass

from voidx.agent.domain.loop import (
    LOOP_ITERATION_USER_TEXT,
    LoopSpec,
    LoopToolView,
    loop_profile_for_spec,
)
from voidx.agent.domain.thread import AgentThread, AgentThreadState, LifecycleState, RuntimeDecision
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.loop.controller import LoopAttemptController
from voidx.agent.runtime.contracts import TurnRequest
from voidx.agent.runtime.dispatcher import DispatchResult, RuntimeDispatcher
from voidx.agent.runtime.pump import WakeupPumpMixin
from voidx.memory.thread_store import ThreadStore
from voidx.runtime.ui import StatusFinished, StatusUpdated, ui_events

LOOP_WAITING_STATUS_ID = "loop:waiting"


def _publish_loop_waiting(decision: RuntimeDecision) -> None:
    """Surface the next-wakeup time so idle UIs can show a countdown."""
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


class LoopRuntimeScheduler(WakeupPumpMixin):
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
        self._session_id = session_id
        self._init_pump(
            lease_owner=lease_owner,
            lease_seconds=lease_seconds,
            pump_poll_seconds=pump_poll_seconds,
        )

    def register_loop_thread(self, thread_id: str) -> None:
        self.register_managed_thread(thread_id)

    def unregister_loop_thread(self, thread_id: str) -> None:
        self.unregister_managed_thread(thread_id)

    def _pump_has_work(self) -> bool:
        return bool(self._managed_thread_ids)

    async def _owns_wakeup(self, thread_id: str) -> bool:
        return thread_id in self._managed_thread_ids

    def _runner(self):
        return LoopRuntimeRunner(self._runtime)

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
            runner=self._runner(),
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
