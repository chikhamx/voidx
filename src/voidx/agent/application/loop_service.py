"""Application service for runtime-backed /loop lifecycle."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from voidx.agent.domain.loop import LOOP_PROFILE, LoopMode, LoopSpec
from voidx.agent.loop.prompt_materialize import materialize_loop_prompt
from voidx.agent.domain.thread import AgentThread, AgentThreadState, LifecycleState, RuntimeDecision
from voidx.memory.service import ensure_session
from voidx.memory.thread_store import ThreadStateConflict, ThreadStore
from voidx.runtime.ui import StatusFinished, ui_events


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


class LoopService:
    def __init__(self, *, store: ThreadStore, scheduler, workspace: str) -> None:
        self._store = store
        self._scheduler = scheduler
        self._workspace = workspace
        self._active_specs: dict[str, LoopSpec] = {}

    async def start(self, parent_thread_id: str | None, spec: LoopSpec) -> LoopStatus:
        parent = _parent_id(parent_thread_id)
        display_text = _display_text(spec.prompt)
        spec = spec.model_copy(
            update={
                "prompt": materialize_loop_prompt(spec.prompt, self._workspace),
                "generation": _new_generation(),
            }
        )
        await self._deactivate_current(parent)
        # Every start opens a fresh loop session; wakeups from any previous
        # generation are poison and must be discarded before the pump sees them.
        await self._store.discard_pending_outbox_prefix(f"loop:{parent}:")
        return await self._activate(parent, spec, display_text)

    async def resume(self, parent_thread_id: str | None) -> LoopStatus | None:
        """Reactivate the most recent loop of this parent with its original session."""
        parent = _parent_id(parent_thread_id)
        if parent in self._active_specs:
            return await self.status(parent)
        loop_thread_id = await self._store.latest_thread_id_with_prefix(f"loop:{parent}:")
        if loop_thread_id is None:
            return None
        loaded = await self._store.load(loop_thread_id)
        if loaded is None:
            return None
        if loaded.state.lifecycle in {
            LifecycleState.COMPLETED,
            LifecycleState.FAILED,
            LifecycleState.CANCELLED,
        }:
            return None
        spec = self._spec_from_state(loaded.state, loop_thread_id)
        if spec is None:
            return None
        loop_session_id = spec.loop_session_id(parent)
        await ensure_session(loop_session_id, self._workspace, profile="loop")
        if loaded.thread.session_id != loop_session_id:
            await self._store.rebind_thread_session(loop_thread_id, loop_session_id)
        self._active_specs[parent] = spec
        register = getattr(self._scheduler, "register_loop_thread", None)
        if callable(register):
            register(loop_thread_id)
        start_pump = getattr(self._scheduler, "start_pump", None)
        if callable(start_pump):
            start_pump()
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
        await ensure_session(loop_session_id, self._workspace, profile="loop")
        loop_thread_id = spec.loop_thread_id(parent)
        await self._ensure_thread(parent, loop_thread_id, spec, session_id=loop_session_id)
        self._active_specs[parent] = spec
        register = getattr(self._scheduler, "register_loop_thread", None)
        if callable(register):
            register(loop_thread_id)
        await self._scheduler.run_prompt(
            spec.prompt,
            display_text=display_text,
            session_id=parent,
            spec=spec,
        )
        start_pump = getattr(self._scheduler, "start_pump", None)
        if callable(start_pump):
            start_pump()
        status = await self.status(parent)
        if status is not None:
            return status
        raise RuntimeError(await self._start_failure_detail(parent, spec))

    async def status(self, parent_thread_id: str | None) -> LoopStatus | None:
        parent = _parent_id(parent_thread_id)
        spec = self._active_specs.get(parent)
        if spec is None:
            return None
        loaded = await self._store.load(spec.loop_thread_id(parent))
        if loaded is None:
            return None
        if loaded.state.lifecycle in {
            LifecycleState.COMPLETED,
            LifecycleState.FAILED,
            LifecycleState.CANCELLED,
        }:
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

    async def _deactivate_current(self, parent: str) -> bool:
        spec = self._active_specs.pop(parent, None)
        if spec is None:
            return False
        loop_thread_id = spec.loop_thread_id(parent)
        unregister = getattr(self._scheduler, "unregister_loop_thread", None)
        if callable(unregister):
            unregister(loop_thread_id)
        ui_events.emit_nowait(StatusFinished(status_id="loop:waiting"))
        for _ in range(2):
            loaded = await self._store.load(loop_thread_id)
            if loaded is None:
                break
            if loaded.state.lifecycle in {
                LifecycleState.COMPLETED,
                LifecycleState.FAILED,
                LifecycleState.CANCELLED,
            }:
                break
            stopped = loaded.state.model_copy(
                update={
                    "lifecycle": LifecycleState.CANCELLED,
                    "lifecycle_decision": RuntimeDecision(
                        outcome="stop",
                        summary="Loop superseded by a new /loop start.",
                        progress="partial",
                    ),
                }
            )
            try:
                await self._store.save_state(
                    loaded.thread.thread_id,
                    stopped,
                    expected_state_version=loaded.state_version,
                )
                break
            except ThreadStateConflict:
                continue
        await self._store.discard_pending_outbox(loop_thread_id)
        return True

    async def stop(self, parent_thread_id: str | None) -> bool:
        parent = _parent_id(parent_thread_id)
        spec = self._active_specs.pop(parent, None)
        if spec is None:
            return False
        unregister = getattr(self._scheduler, "unregister_loop_thread", None)
        if callable(unregister):
            unregister(spec.loop_thread_id(parent))
        ui_events.emit_nowait(StatusFinished(status_id="loop:waiting"))
        loaded = await self._store.load(spec.loop_thread_id(parent))
        if loaded is None:
            return False
        if loaded.state.lifecycle in {
            LifecycleState.COMPLETED,
            LifecycleState.FAILED,
            LifecycleState.CANCELLED,
        }:
            return True
        stopped = loaded.state.model_copy(
            update={
                "lifecycle": LifecycleState.CANCELLED,
                "lifecycle_decision": RuntimeDecision(
                    outcome="stop",
                    summary="Loop stopped by user.",
                    progress="partial",
                ),
            }
        )
        ui_events.emit_nowait(StatusFinished(status_id="loop:waiting"))
        await self._store.save_state(
            loaded.thread.thread_id,
            stopped,
            expected_state_version=loaded.state_version,
        )
        # A stopped loop must not leave wakeups behind for the pump to retry forever.
        await self._store.discard_pending_outbox(loaded.thread.thread_id)
        if not self._active_specs:
            stop_pump = getattr(self._scheduler, "stop_pump", None)
            if callable(stop_pump):
                await stop_pump()
        return True

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
                profile=LOOP_PROFILE,
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


def _new_generation() -> str:
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"


def _parent_id(parent_thread_id: str | None) -> str:
    return (parent_thread_id or "default").strip() or "default"


def _display_text(prompt: str) -> str:
    return f"[loop] {prompt.replace(chr(10), ' ')[:80]}"


def _next_delay(spec: LoopSpec, decision: RuntimeDecision | None) -> float | None:
    if spec.mode is LoopMode.FIXED:
        return spec.interval_seconds
    if decision is None:
        return None
    return decision.next_delay_seconds
