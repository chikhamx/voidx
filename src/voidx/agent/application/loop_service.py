"""Application service for runtime-backed /loop lifecycle."""

from __future__ import annotations

from dataclasses import dataclass

from voidx.agent.domain.loop import LOOP_PROFILE, LoopMode, LoopSpec
from voidx.agent.domain.thread import AgentThread, AgentThreadState, LifecycleState, RuntimeDecision
from voidx.memory.thread_store import ThreadStore


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
        loop_thread_id = spec.loop_thread_id(parent)
        await self._ensure_thread(parent, loop_thread_id, spec)
        self._active_specs[parent] = spec
        await self._scheduler.run_prompt(
            spec.prompt,
            display_text=_display_text(spec.prompt),
            session_id=parent,
            spec=spec,
        )
        status = await self.status(parent)
        if status is None:
            raise RuntimeError("loop failed to start")
        return status

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

    async def stop(self, parent_thread_id: str | None) -> bool:
        parent = _parent_id(parent_thread_id)
        spec = self._active_specs.pop(parent, None)
        if spec is None:
            return False
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
        await self._store.save_state(
            loaded.thread.thread_id,
            stopped,
            expected_state_version=loaded.state_version,
        )
        return True

    async def _ensure_thread(
        self, parent_thread_id: str, loop_thread_id: str, spec: LoopSpec
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
            },
        )
        if loaded is None:
            return await self._store.create_thread(
                AgentThread(
                    thread_id=loop_thread_id,
                    session_id=parent_thread_id,
                    parent_thread_id=parent_thread_id,
                    workspace=self._workspace,
                ),
                profile=LOOP_PROFILE,
                state=state,
                resource_scope={"workspace": self._workspace},
            )
        return await self._store.save_state(
            loop_thread_id,
            state,
            expected_state_version=loaded.state_version,
        )


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
