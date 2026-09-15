"""Run-wide producer and turn lifecycle ownership."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from contextvars import ContextVar
from contextlib import contextmanager
from typing import Any, Literal

from voidx.agent.application.runtime.run_event_mux import RunEventMux
from voidx.agent.application.runtime.run_supervisor import RunSupervisor
from voidx.agent.domain import semantic_events as e
from voidx.observability import log_internal_error


_RENEWAL: ContextVar[object | None] = ContextVar("run_renewal", default=None)


_DISPATCH: ContextVar[object | None] = ContextVar("run_dispatch", default=None)


_CURRENT_OWNER: ContextVar[object | None] = ContextVar("run_owner", default=None)


class SharedInteractionBudget:
    def __init__(self, capacity: int) -> None:
        if type(capacity) is not int or capacity <= 0:
            raise ValueError("Interaction budget must be a positive integer")
        self.capacity = capacity
        self.count = 0
        self.closed = False
        self._changed = asyncio.Event()

    async def acquire(self) -> None:
        while True:
            if self.closed:
                raise RuntimeError("Run is not accepting interactions")
            if self.count < self.capacity:
                self.count += 1
                return
            self._changed.clear()
            await self._changed.wait()

    def release(self) -> None:
        self.count -= 1
        self._changed.set()

    def begin_cancel(self) -> None:
        self.closed = True
        self._changed.set()


class RunOwner:
    def __init__(self, *, turns: int = 8, producers: int = 32, capacity: int = 256, interactions: int = 64, cleanup=None) -> None:
        if type(producers) is not int or producers < 2 * turns + 2:
            raise ValueError("Producer budget requires P >= 2T + 2")
        self.interactions = SharedInteractionBudget(interactions)
        self.mux = RunEventMux(turns=turns, capacity=capacity, release_tail=self.interactions.release)
        self._limits = {"execution": producers - turns - 2, "renewal": turns, "pump": 2}
        self._counts = dict.fromkeys(self._limits, 0)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._executions: set[asyncio.Task[Any]] = set()
        self._dispatches: set[asyncio.Task[Any]] = set()
        self._supervisors: set[RunSupervisor] = set()
        self._committing: dict[asyncio.Task, set[RunSupervisor]] = {}
        self._stopping = False
        self._failed = False
        self._finishing = False
        self._completed = False
        self._cleanup = cleanup
        self._cleanup_error_limit = producers + turns + interactions + 4
        self._cleanup_errors: list[BaseException] = []
        self._cleanup_errors_dropped = 0
        self._cleanup_errors_dropped_by_source = {"turn": 0, "owner": 0}
        self._cleanup_error_group: BaseExceptionGroup | None = None
        self._stop = asyncio.Event()
        self._lifecycle: asyncio.Task[None] | None = None

    def record_cleanup_error(self, error: BaseException) -> None:
        self._record_cleanup_error(error, source="turn")

    def _record_cleanup_error(self, error: BaseException, *, source: Literal["turn", "owner"]) -> None:
        self._failed = True
        if isinstance(error, BaseExceptionGroup):
            for child in error.exceptions:
                self._record_cleanup_error(child, source=source)
        elif len(self._cleanup_errors) < self._cleanup_error_limit:
            self._cleanup_errors.append(error)
        else:
            self._cleanup_errors_dropped += 1
            self._cleanup_errors_dropped_by_source[source] += 1

    def _raise_cleanup_errors(self) -> None:
        if self._cleanup_errors:
            if self._cleanup_error_group is None:
                self._cleanup_error_group = BaseExceptionGroup("Owned run cleanup failed", self._cleanup_errors)
            raise self._cleanup_error_group

    def set_cleanup(self, cleanup) -> None:
        if self._lifecycle is not None:
            raise RuntimeError("Run lifecycle already started")
        self._cleanup = cleanup

    @property
    def accepting_dispatches(self) -> bool:
        return not (self._finishing or self._stopping)

    @property
    def producer_count(self) -> int:
        return len(self._tasks) + len(self._supervisors)

    @contextmanager
    def reserve_dispatch(self):
        if self._stopping or (self._finishing and _CURRENT_OWNER.get() is not self):
            raise RuntimeError("Run is not accepting producers")
        execution = _CURRENT_OWNER.get() is not self
        if (self._counts["renewal"] >= self._limits["renewal"] or
                execution and self._counts["execution"] >= self._limits["execution"]):
            raise RuntimeError("Nested dispatch capacity exhausted")
        self._counts["renewal"] += 1
        self._counts["execution"] += int(execution)
        reservation = [self, False]
        token = _RENEWAL.set(reservation)
        owner_token = _CURRENT_OWNER.set(self)
        task = asyncio.current_task()
        self._dispatches.add(task)
        dispatch = [task, None]
        dispatch_token = _DISPATCH.set(dispatch)
        try:
            yield
        except BaseException as exc:
            if dispatch[1] is not None:
                dispatch[1].set_exception(exc)
            raise
        else:
            if dispatch[1] is not None:
                dispatch[1].set_result(None)
        finally:
            self._committing.pop(task, None)
            _DISPATCH.reset(dispatch_token)
            _RENEWAL.reset(token)
            _CURRENT_OWNER.reset(owner_token)
            self._dispatches.discard(task)
            self._counts["renewal"] -= 1
            self._counts["execution"] -= int(execution)
            self.mux.notify()

    async def complete_after_dispatch(self, supervisor, ready) -> None:
        dispatch = _DISPATCH.get()
        if dispatch is None:
            return
        if dispatch[1] is None:
            dispatch[1] = asyncio.get_running_loop().create_future()
        self._committing.setdefault(dispatch[0], set()).add(supervisor)
        ready.set_result(None)
        await dispatch[1]

    def dispatch_channel(self):
        dispatch = _DISPATCH.get()
        supervisors = self._committing.get(dispatch[0], ()) if dispatch is not None else ()
        if not supervisors:
            return None
        if len(supervisors) != 1:
            raise RuntimeError("Scheduler output requires exactly one pending child turn")
        return next(iter(supervisors)).channel

    def in_dispatch(self) -> bool:
        return _DISPATCH.get() is not None

    def spawn(self, factory: Callable[[], Coroutine[Any, Any, Any]], *,
              role: Literal["execution", "renewal", "pump"] = "execution") -> asyncio.Task[Any]:
        if self._stopping or (self._finishing and _CURRENT_OWNER.get() is not self):
            raise RuntimeError("Run is not accepting producers")
        if role not in self._limits:
            raise ValueError("Unknown producer role")
        reservation = _RENEWAL.get() if role == "renewal" else None
        reserved = reservation is not None and reservation[0] is self and not reservation[1]
        if not reserved and self._counts[role] >= self._limits[role]:
            raise RuntimeError("Nested producer capacity exhausted")
        if reserved:
            reservation[1] = True
        else:
            self._counts[role] += 1
        try:
            token = _CURRENT_OWNER.set(self) if role == "execution" else None
            try:
                task = asyncio.create_task(factory(), name=f"run-{role}")
            finally:
                if token is not None:
                    _CURRENT_OWNER.reset(token)
        except BaseException:
            if not reserved:
                self._counts[role] -= 1
            raise
        self._tasks.add(task)
        if role == "execution":
            self._executions.add(task)
        task.add_done_callback(lambda task: self._done(task, role, reserved))
        self._start()
        return task

    async def register_turn(self, *, session_id: str, thread_id: str,
                            execute, persist, cleanup, before_cancel=None,
                            nested: bool = False) -> RunSupervisor:
        nested = nested or _CURRENT_OWNER.get() is self
        while True:
            if self._stopping or (self._finishing and _CURRENT_OWNER.get() is not self):
                raise RuntimeError("Run is not accepting turns")
            if (self._counts["execution"] < self._limits["execution"]
                    and self.mux.registered < self.mux.turns):
                # register cannot suspend after the joint capacity check.
                channel = await self.mux.register(session_id=session_id, thread_id=thread_id, nested=True)
                break
            if nested:
                raise RuntimeError("Nested turn/producer capacity exhausted")
            self.mux._changed.clear()
            await self.mux._changed.wait()
        supervisor = RunSupervisor(channel, execute=execute, persist=persist, cleanup=cleanup,
                                   before_cancel=before_cancel, owner=self)
        self._counts["execution"] += 1
        self._supervisors.add(supervisor)
        token = _CURRENT_OWNER.set(self)
        try:
            supervisor.start()
        finally:
            _CURRENT_OWNER.reset(token)
        assert supervisor._task is not None
        supervisor._task.add_done_callback(lambda task: self._turn_done(supervisor, task))
        self._start()
        return supervisor

    def _turn_done(self, supervisor: RunSupervisor, task: asyncio.Task[None]) -> None:
        self._supervisors.discard(supervisor)
        self._counts["execution"] -= 1
        self.mux.notify()
        if task.cancelled() or task.exception() is not None:
            self._failed = True
            self.begin_cancel()
        elif e.decode_event(supervisor.channel._completion.result().terminal).kind == "turn.failed":
            self._failed = True
            self.begin_cancel()

    def _start(self) -> None:
        if self._lifecycle is None:
            self._lifecycle = asyncio.create_task(self._drive(), name="run-owner")

    def _done(self, task: asyncio.Task[Any], role: str, reserved: bool = False) -> None:
        self._tasks.discard(task)
        self._executions.discard(task)
        if not reserved:
            self._counts[role] -= 1
        self.mux.notify()
        if not task.cancelled() and task.exception() is not None:
            self._failed = True
            self.begin_cancel()

    def begin_cancel(self) -> None:
        self._stopping = True
        self.interactions.begin_cancel()
        for supervisor in self._supervisors:
            if not any(supervisor in turns for turns in self._committing.values()):
                supervisor.begin_cancel()
        self.mux.begin_cancel()
        self._stop.set()

    async def _drive(self) -> None:
        await self._stop.wait()
        if self._finishing and not self._stopping:
            while self._supervisors or self._executions or self._dispatches:
                for supervisor in tuple(self._supervisors):
                    await supervisor.wait()
                if self._executions or self._dispatches:
                    await asyncio.gather(*tuple(self._executions | self._dispatches), return_exceptions=True)
                # Completed task callbacks must retire their ownership before rechecking.
                await asyncio.sleep(0)
            self._completed = not (self._failed or self._stopping)
            self.begin_cancel()
        tasks = tuple(self._tasks | self._dispatches)
        for task in tasks:
            if task not in self._committing:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for turns in self._committing.values():
            for supervisor in turns:
                await supervisor.wait()
        self._committing.clear()
        for supervisor in tuple(self._supervisors):
            await supervisor.cancel()
        try:
            if self._cleanup is not None:
                await self._cleanup()
        except BaseException as exc:
            log_internal_error(exc, context="run_owner_cleanup")
            self._record_cleanup_error(exc, source="owner")
        finally:
            if self._cleanup_errors_dropped:
                log_internal_error(
                    RuntimeError(f"Additional cleanup errors omitted: {self._cleanup_errors_dropped_by_source}"),
                    context="run_owner_cleanup_overflow",
                )
            self.mux.finish("failed" if self._failed else "completed" if self._completed else "cancelled")

    async def _terminate(self) -> None:
        """Release stream resources without reporting stored explicit-close errors."""
        self.begin_cancel()
        self._start()
        await asyncio.shield(self._lifecycle)

    async def cancel(self) -> None:
        await self._terminate()
        self._raise_cleanup_errors()

    def request_finish(self, *, failed: bool = False) -> None:
        self._failed = self._failed or failed
        self._finishing = True
        self.mux.notify()
        self._stop.set()
        self._start()

    async def finish(self) -> None:
        self.request_finish()
        await asyncio.shield(self._lifecycle)
        self._raise_cleanup_errors()

    async def aclose(self) -> None:
        await self.cancel()

    @property
    def completion(self):
        return self.mux.completion

    def events(self):
        return self.mux.events()
