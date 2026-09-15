"""Task-scoped guidance capability shared only by bootstrap composition."""
import asyncio
from contextvars import ContextVar


current_guidance = ContextVar("managed_guidance", default=None)


class ManagedGuidance:
    def __init__(self):
        self._ready = asyncio.Event()
        self._service = None
        self._target = None
        self._closed = False

    def activate(self, service, context):
        if self._closed:
            raise RuntimeError("Guidance owner is closed")
        profile = context.runtime_profile.profile_id
        if profile == "goal" and context.goal_phase == "evaluator":
            return
        if profile == "goal" and context.goal_phase != "idle":
            target = dict(session_id=context.goal_work_session_id,
                          thread_id=context.thread_id,
                          run_id=context.goal_generation, phase="work")
        else:
            target = dict(session_id=context.session_id, thread_id=context.thread_id,
                          phase=context.goal_phase or context.loop_phase or "work")
        self._service = service
        self._target = target
        self._ready.set()

    def activate_recovery(self, service, binding):
        if self._closed:
            raise RuntimeError("Guidance owner is closed")
        self._service = service
        self._target = dict(session_id=binding.work_session_id,
                            thread_id=binding.goal_thread_id,
                            run_id=binding.generation, phase="work")
        self._ready.set()

    async def submit(self, text):
        await self._ready.wait()
        if self._closed:
            raise RuntimeError("Guidance owner closed before delivery")
        return self._service.submit_guidance(text, **self._target)

    def close(self):
        self._closed = True
        self._ready.set()
