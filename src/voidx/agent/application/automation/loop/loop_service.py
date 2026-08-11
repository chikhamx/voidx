"""Application service for runtime-backed /loop lifecycle."""

from __future__ import annotations

from dataclasses import dataclass

from voidx.agent.application.autonomous import (
    AutonomousServiceBase,
    LoopScheduler,
    new_generation,
    parent_id,
)
from voidx.agent.domain.automation.loop import LoopMode, LoopSpec, loop_profile_for_spec
from voidx.agent.application.automation.loop.prompt_materialize import materialize_loop_prompt
from voidx.agent.domain.thread import (
    TERMINAL_LIFECYCLES,
    AgentThread,
    AgentThreadState,
    LifecycleState,
    RuntimeDecision,
)
from voidx.agent.ports.persistence import ThreadStore
from voidx.agent.ports.presentation import AgentEventPublisher, NullAgentEventPublisher


@dataclass(frozen=True)
class LoopStatus:
    active: bool
    parent_thread_id: str
    loop_thread_id: str
    mode: str
    interval_seconds: float | None
    iteration: int
    next_wakeup_in_seconds: float | None
    prompt_summary: str
    last_summary: str
    last_error: str
    state: str


class LoopService(AutonomousServiceBase[LoopSpec, LoopScheduler]):
    def __init__(
        self,
        *,
        store: ThreadStore,
        scheduler: LoopScheduler,
        workspace: str,
        events: AgentEventPublisher | None = None,
    ) -> None:
        super().__init__(store=store, scheduler=scheduler, workspace=workspace)
        self._events = events or NullAgentEventPublisher()

    def _spec_thread_id(self, spec: LoopSpec, parent: str) -> str:
        return spec.loop_thread_id(parent)

    def _register_thread(self, thread_id: str) -> None:
        self._scheduler.register_loop_thread(thread_id)

    def _unregister_thread(self, thread_id: str) -> None:
        self._scheduler.unregister_loop_thread(thread_id)

    def _on_deactivated(self) -> None:
        self._events.clear_loop_waiting()

    async def start(self, parent_thread_id: str | None, spec: LoopSpec) -> LoopStatus:
        parent = parent_id(parent_thread_id)
        async with self._lock_for(parent):
            return await self._start_unlocked(parent, spec)

    async def _start_unlocked(self, parent: str, spec: LoopSpec) -> LoopStatus:
        display_text = _display_text(spec.prompt)
        spec = spec.model_copy(
            update={
                "prompt": materialize_loop_prompt(spec.prompt, self._workspace),
                "generation": new_generation(),
            }
        )
        await self._deactivate_current(parent, summary="Loop superseded by a new /loop start.")
        await self._store.discard_pending_outbox_prefix(f"loop:{parent}:")
        return await self._activate(parent, spec, display_text)

    async def resume(self, parent_thread_id: str | None) -> LoopStatus | None:
        parent = parent_id(parent_thread_id)
        async with self._lock_for(parent):
            return await self._resume_unlocked(parent)

    async def _resume_unlocked(self, parent: str) -> LoopStatus | None:
        """Reactivate the most recent loop of this parent with its original session."""
        if parent in self._active_specs:
            return await self.status(parent)
        loop_thread_id = await self._store.latest_thread_id_with_prefix(f"loop:{parent}:")
        if loop_thread_id is None:
            return None
        loaded = await self._store.load(loop_thread_id)
        if loaded is None:
            return None
        if loaded.state.lifecycle in TERMINAL_LIFECYCLES:
            return None
        spec = self._spec_from_state(loaded.state, loop_thread_id)
        if spec is None:
            return None
        loop_session_id = spec.loop_session_id(parent)
        await self._store.ensure_session(loop_session_id, self._workspace, profile="loop")
        if loaded.thread.session_id != loop_session_id:
            await self._store.rebind_thread_session(loop_thread_id, loop_session_id)
        self._active_specs[parent] = spec
        self._register_thread(loop_thread_id)
        self._start_pump()
        return await self.status(parent)

    @staticmethod
    def _spec_from_state(state, loop_thread_id: str) -> LoopSpec | None:
        context = state.context if isinstance(state.context, dict) else {}
        raw = context.get("loop_spec")
        if isinstance(raw, dict):
            return LoopSpec.model_validate(raw)
        prompt = str(context.get("prompt") or "").strip()
        if not prompt:
            return None
        return LoopSpec(
            prompt=prompt,
            interval_seconds=context.get("interval_seconds"),
            generation=loop_thread_id.rsplit(":", 1)[-1],
        )

    async def _activate(self, parent: str, spec: LoopSpec, display_text: str) -> LoopStatus:
        loop_session_id = spec.loop_session_id(parent)
        await self._store.ensure_session(
            loop_session_id,
            self._workspace,
            profile="loop",
            root_session_id=parent,
        )
        loop_thread_id = spec.loop_thread_id(parent)
        await self._ensure_thread(parent, loop_thread_id, spec, session_id=loop_session_id)
        self._active_specs[parent] = spec
        self._register_thread(loop_thread_id)
        await self._scheduler.run_prompt(
            spec.prompt,
            display_text=display_text,
            session_id=parent,
            spec=spec,
        )
        self._start_pump()
        status = await self.status(parent)
        if status is not None:
            return status
        raise RuntimeError(await self._start_failure_detail(parent, spec))

    async def status(self, parent_thread_id: str | None) -> LoopStatus | None:
        parent = parent_id(parent_thread_id)
        spec = self._active_specs.get(parent)
        if spec is None:
            return None
        loaded = await self._store.load(spec.loop_thread_id(parent))
        if loaded is None:
            return None
        if loaded.state.lifecycle in TERMINAL_LIFECYCLES:
            self._active_specs.pop(parent, None)
            return None
        decision = loaded.state.lifecycle_decision
        return LoopStatus(
            active=True,
            parent_thread_id=parent,
            loop_thread_id=loaded.thread.thread_id,
            mode=spec.mode.value,
            interval_seconds=spec.interval_seconds,
            iteration=int(loaded.state.context.get("iteration", 0) or 0),
            next_wakeup_in_seconds=_next_delay(spec, decision),
            prompt_summary=spec.prompt_summary(),
            last_summary=decision.summary if decision is not None else "",
            last_error=decision.reason if decision is not None and decision.outcome == "failed" else "",
            state=loaded.state.lifecycle.value,
        )

    async def stop(self, parent_thread_id: str | None) -> bool:
        parent = parent_id(parent_thread_id)
        async with self._lock_for(parent):
            return await self._stop_unlocked(parent)

    async def _stop_unlocked(self, parent: str) -> bool:
        deactivated = await self._deactivate_current(parent, summary="Loop stopped by user.")
        # A stopped loop must not leave wakeups behind for the pump to retry forever.
        if deactivated and not self._active_specs:
            await self._stop_pump()
        return deactivated

    async def _start_failure_detail(self, parent: str, spec: LoopSpec) -> str:
        loaded = await self._store.load(spec.loop_thread_id(parent))
        if loaded is None:
            return "loop failed to start"
        decision = loaded.state.lifecycle_decision
        if loaded.state.lifecycle is LifecycleState.FAILED:
            detail = ""
            if decision is not None:
                detail = decision.reason or decision.summary
            return f"loop failed during first iteration: {detail or 'unknown error'}"
        summary = decision.summary if decision is not None else ""
        return f"loop ended during first iteration: {summary}"

    async def _ensure_thread(
        self, parent_thread_id: str, loop_thread_id: str, spec: LoopSpec, *, session_id: str
    ):
        loaded = await self._store.load(loop_thread_id)
        state = AgentThreadState(
            thread_id=loop_thread_id,
            lifecycle=LifecycleState.WAITING,
            context={
                "iteration": 0,
                "prompt": spec.prompt,
                "mode": spec.mode.value,
                "interval_seconds": spec.interval_seconds,
                "loop_spec": spec.model_dump(mode="json"),
            },
        )
        if loaded is None:
            return await self._store.create_thread(
                AgentThread(
                    thread_id=loop_thread_id,
                    session_id=session_id,
                    parent_thread_id=parent_thread_id,
                    workspace=self._workspace,
                ),
                profile=loop_profile_for_spec(spec),
                state=state,
                resource_scope={"workspace": self._workspace},
            )
        if loaded.thread.session_id != session_id:
            # Migrate pre-isolation threads that were bound to the parent session.
            await self._store.rebind_thread_session(loop_thread_id, session_id)
        return await self._store.save_state(
            loop_thread_id,
            state,
            expected_state_version=loaded.state_version,
        )


def _display_text(prompt: str) -> str:
    return f"[loop] {prompt.replace(chr(10), ' ')[:80]}"


def _next_delay(spec: LoopSpec, decision: RuntimeDecision | None) -> float | None:
    if spec.mode is LoopMode.FIXED:
        return spec.interval_seconds
    if decision is None:
        return None
    return decision.next_delay_seconds
