"""Runtime-backed execution adapter for scheduled /loop prompts."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from voidx.agent.domain.automation.loop import (
    LOOP_ITERATION_USER_TEXT,
    LoopSpec,
    LoopToolView,
    loop_profile_for_base,
)
from voidx.agent.domain.agent_profile import ResolvedAgentProfile
from voidx.agent.domain.thread import AgentThread, AgentThreadState, LifecycleState, RuntimeDecision
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.application.agent_registry import AgentRegistry
from voidx.agent.application.automation.loop.controller import LoopAttemptController
from voidx.agent.application.runtime.contracts import TurnRequest
from voidx.agent.application.profile_tool_policy import profile_tool_policy_for
from voidx.agent.application.runtime.dispatcher import DispatchResult, RuntimeDispatcher
from voidx.agent.application.runtime.pump import WakeupPumpMixin
from voidx.agent.ports.persistence import ThreadStore
from voidx.agent.ports.presentation import AgentEventPublisher, NullAgentEventPublisher

LOOP_WAITING_STATUS_ID = "loop:waiting"


def _publish_loop_waiting(decision: RuntimeDecision, events: AgentEventPublisher) -> None:
    """Surface the next-wakeup time so idle UIs can show a countdown."""
    if decision.outcome == "continue" and decision.next_delay_seconds:
        events.show_loop_waiting(time.time() + float(decision.next_delay_seconds))
    else:
        events.clear_loop_waiting()


_DEFAULT_DYNAMIC_DELAY_SECONDS = 600.0


@dataclass(frozen=True)
class LoopRuntimeRunner:
    runtime: object
    events: AgentEventPublisher

    async def run_turn(
        self, *, thread, profile: ResolvedAgentProfile, input_frame: dict
    ) -> RuntimeDecision:
        prompt = str(input_frame.get("prompt", ""))
        if not prompt.strip():
            return RuntimeDecision(
                outcome="failed",
                summary="Loop prompt was empty.",
                reason="empty_loop_prompt",
            )
        spec = LoopSpec.model_validate(input_frame.get("spec") or {"prompt": prompt})
        controller = LoopAttemptController(spec=spec)
        self.events.clear_loop_waiting()
        context = TurnExecutionContext(
            thread_id=thread.thread_id,
            session_id=thread.session_id or "",
            runtime_profile=profile.runtime_profile,
            workflow_context=profile.workflow_context,
            workspace=thread.workspace,
            tool_policy=profile_tool_policy_for(
                profile,
                baseline=LoopToolView.default(
                    workflow_enabled=spec.workflow_enabled
                ).bind(_available_loop_tool_ids()),
                phase="work",
            ),
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
                guidance=tuple(input_frame.get("guidance") or ()),
            )
        )
        submitted = controller.final_decision()
        if submitted is not None:
            _publish_loop_waiting(submitted, self.events)
            return submitted
        fallback = await controller.submit_decision(
            RuntimeDecision(
                outcome="continue",
                summary="Iteration ended without a loop decision; continuing with the default delay.",
                next_delay_seconds=None if spec.interval_seconds is not None else _DEFAULT_DYNAMIC_DELAY_SECONDS,
                reason="no_loop_decision_submitted",
            )
        )
        _publish_loop_waiting(fallback, self.events)
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
        events: AgentEventPublisher | None = None,
        guidance: Any | None = None,
    ) -> None:
        self._store = store
        self._runtime = runtime
        self._workspace = workspace
        self._session_id = session_id
        self._events = events or NullAgentEventPublisher()
        self._guidance = guidance
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

    def _claim_wakeup_filters(self) -> dict:
        return {"thread_id_prefix": "loop:"}

    async def _owns_wakeup(self, thread_id: str) -> bool:
        if not self._managed_thread_ids:
            return False
        if thread_id in self._managed_thread_ids:
            return True
        if not self._session_id or not thread_id.startswith(f"loop:{self._session_id}:"):
            return False
        loaded = await self._store.load(thread_id)
        return loaded is not None and loaded.profile.protocol == "loop"

    def _on_wakeup_owned(self, outbox) -> None:
        self._managed_thread_ids.add(outbox.thread_id)

    def _runner(self):
        return LoopRuntimeRunner(self._runtime, self._events)

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
            events=self._events,
            guidance=self._guidance,
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
            profile=_loop_resolved_profile(self._workspace, spec),
            state=AgentThreadState(thread_id=thread_id, lifecycle=LifecycleState.READY),
            resource_scope={"workspace": self._workspace},
        )


def _loop_resolved_profile(workspace: str, spec: LoopSpec) -> ResolvedAgentProfile:
    resolved = AgentRegistry(workspace).resolve("loop")
    return resolved.model_copy(update={
        "runtime_profile": loop_profile_for_base(resolved.runtime_profile, spec)
    })


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
        "todo",
    }
