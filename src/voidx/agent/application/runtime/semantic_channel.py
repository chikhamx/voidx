"""Single-consumer bounded JSON channel; only the supervisor seals completion."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

from voidx.agent.domain import semantic_events as e
from voidx.agent.ports.run_lifecycle import EventBudget

_TERMINALS = {"turn.completed", "turn.failed", "turn.cancelled"}
MAX_COMPLETION_TAIL = 64


@dataclass(frozen=True)
class RunCompletion:
    tail: tuple[bytes, ...]
    terminal: bytes


class SemanticChannel:
    def __init__(
        self, *, session_id: str, thread_id: str, turn_id: str,
        capacity: int = e.DEFAULT_QUEUE_CAPACITY,
        max_event_bytes: int = e.DEFAULT_MAX_EVENT_BYTES,
        budget: EventBudget | None = None,
    ) -> None:
        for name, value in (("capacity", capacity), ("max_event_bytes", max_event_bytes)):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.identity = dict(session_id=session_id, thread_id=thread_id, turn_id=turn_id)
        if any(not isinstance(value, str) or not value.strip() for value in self.identity.values()):
            raise ValueError("Run identity must be nonempty")
        self.capacity = capacity
        self.max_event_bytes = max_event_bytes
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=capacity)
        self._completion: asyncio.Future[RunCompletion] = asyncio.get_running_loop().create_future()
        self._ready = asyncio.Event()
        self._lock = asyncio.Lock()
        self._sequence = 0
        self._started = False
        self._closed = False
        self._reading = False
        self._delivered = False
        self._tail_index = 0
        self._budget = budget
        self._interaction_cleanups = []

    @property
    def completion(self) -> asyncio.Future[RunCompletion]:
        """Shielded observation; cancellation cannot cancel the owned future."""
        return asyncio.shield(self._completion)

    def _snapshot(self, event: e.SemanticEvent, sequence: int) -> bytes:
        if any(getattr(event, name) != value for name, value in self.identity.items()):
            raise ValueError("Event belongs to another run")
        return e.encode_event(event.model_copy(update={"sequence": sequence}), max_event_bytes=self.max_event_bytes)

    async def publish(self, event: e.SemanticEvent) -> None:
        if self._closed:
            raise RuntimeError("Run no longer accepts events")
        if event.kind in _TERMINALS:
            raise ValueError("Only the supervisor may write a terminal")
        # Freeze before waiting for other publishers or queue capacity.
        snapshot = self._snapshot(event, self._sequence + 1)
        if self._budget is not None:
            await self._budget.acquire()
        accepted = False
        try:
            async with self._lock:
                if self._closed:
                    raise RuntimeError("Run no longer accepts events")
                if event.kind == "turn.started" and self._started:
                    raise ValueError("Turn already started")
                snapshot = self._snapshot(
                    e.decode_event(snapshot, max_event_bytes=self.max_event_bytes), self._sequence + 1,
                )
                await self._queue.put(snapshot)
                accepted = True
                if event.kind == "turn.started":
                    self._started = True
                self._sequence += 1
                self._notify()
        finally:
            if not accepted and self._budget is not None:
                self._budget.release()

    def _notify(self) -> None:
        self._ready.set()
        if self._budget is not None:
            self._budget.notify()

    def _stop_publishing(self) -> None:
        """Called after all managed producers have stopped and been awaited."""
        self._closed = True

    def _finish(self, terminal: e.SemanticEvent, tail: Sequence[e.SemanticEvent] = ()) -> None:
        if self._completion.done():
            raise RuntimeError("Completion already sealed")
        if terminal.kind not in _TERMINALS:
            raise ValueError("Expected a terminal event")
        if len(tail) > MAX_COMPLETION_TAIL:
            raise ValueError("Completion tail exceeds 64 events")
        if any(item.kind != "interaction.resolved" for item in tail):
            raise ValueError("Completion tail accepts only interaction.resolved")
        snapshots = tuple(self._snapshot(item, self._sequence + i + 1) for i, item in enumerate(tail))
        final = self._snapshot(terminal, self._sequence + len(tail) + 1)
        self._closed = True
        self._sequence += len(tail) + 1
        self._completion.set_result(RunCompletion(snapshots, final))
        self._notify()

    @property
    def drained(self) -> bool:
        return self._delivered

    def take_ready(self) -> e.SemanticEvent | None:
        """Take at most one event without allocating a waiter or producer task."""
        if self._delivered:
            return None
        if not self._queue.empty():
            snapshot = self._queue.get_nowait()
            if self._budget is not None:
                self._budget.release()
        elif self._completion.done():
            completion = self._completion.result()
            index = self._tail_index
            if index < len(completion.tail):
                snapshot = completion.tail[index]
                self._tail_index = index + 1
            else:
                snapshot = completion.terminal
                self._delivered = True
        else:
            return None
        return e.decode_event(snapshot, max_event_bytes=self.max_event_bytes)

    async def events(self) -> AsyncIterator[e.SemanticEvent]:
        if self._reading:
            raise RuntimeError("Channel supports one consumer")
        self._reading = True
        try:
            while not self._delivered:
                event = self.take_ready()
                if event is not None:
                    yield event
                else:
                    # No await between checking state and clearing: no lost wakeup.
                    self._ready.clear()
                    await self._ready.wait()
        finally:
            self._reading = False
