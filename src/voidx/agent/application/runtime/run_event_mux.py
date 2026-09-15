"""Bounded aggregation of fixed-identity semantic channels."""
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from voidx.agent.application.runtime.semantic_channel import SemanticChannel
from voidx.agent.domain import semantic_events as e


@dataclass(frozen=True)
class RunCompletion:
    outcome: Literal["completed", "failed", "cancelled"]
    summary: str | None = None


class RunEventMux:
    def __init__(self, *, capacity: int = 256, turns: int = 8,
                 max_event_bytes: int = e.DEFAULT_MAX_EVENT_BYTES,
                 release_tail: Callable[[], None] | None = None) -> None:
        for value in (capacity, turns, max_event_bytes):
            if type(value) is not int or value <= 0:
                raise ValueError("Run budgets must be positive integers")
        self._release_tail = release_tail
        self.capacity, self.turns = capacity, turns
        self.max_event_bytes = max_event_bytes
        self.queued = 0
        self._channels: deque[SemanticChannel] = deque()
        self._changed = asyncio.Event()
        self._closed = False
        self._reading = False
        self._completion: asyncio.Future[RunCompletion] = asyncio.get_running_loop().create_future()

    @property
    def registered(self) -> int:
        return len(self._channels)

    @property
    def completion(self) -> asyncio.Future[RunCompletion]:
        return asyncio.shield(self._completion)

    def notify(self) -> None:
        self._changed.set()

    async def acquire(self) -> None:
        while True:
            if self._closed:
                raise RuntimeError("Run is not accepting events")
            if self.queued < self.capacity:
                self.queued += 1
                return
            self._changed.clear()
            await self._changed.wait()

    def release(self) -> None:
        self.queued -= 1
        self.notify()

    async def register(self, *, session_id: str, thread_id: str,
                       nested: bool = False) -> SemanticChannel:
        while True:
            if self._closed:
                raise RuntimeError("Run is not accepting turns")
            if self.registered < self.turns:
                channel = SemanticChannel(
                    session_id=session_id, thread_id=thread_id, turn_id=str(uuid4()),
                    capacity=self.capacity, max_event_bytes=self.max_event_bytes, budget=self,
                )
                self._channels.append(channel)
                self.notify()
                return channel
            if nested:
                raise RuntimeError("Nested turn capacity exhausted")
            self._changed.clear()
            await self._changed.wait()

    def begin_cancel(self) -> None:
        self._closed = True
        self.notify()

    def finish(self, outcome: Literal["completed", "failed", "cancelled"]) -> None:
        if self._completion.done():
            return
        if any(not channel._completion.done() for channel in self._channels):
            raise RuntimeError("Run still has unsealed turns")
        self.begin_cancel()
        self._completion.set_result(RunCompletion(outcome, "Run failed" if outcome == "failed" else None))

    async def events(self) -> AsyncIterator[e.SemanticEvent]:
        if self._reading:
            raise RuntimeError("Mux supports one consumer")
        self._reading = True
        try:
            while True:
                selected = None
                for _ in range(self.registered):
                    channel = self._channels.popleft()
                    tail_index = channel._tail_index
                    selected = channel.take_ready()
                    if channel._tail_index > tail_index and self._release_tail is not None:
                        self._release_tail()
                    if channel.drained:
                        self.notify()
                    else:
                        self._channels.append(channel)
                    if selected is not None:
                        break
                if selected is not None:
                    yield selected
                elif not self._channels and self._completion.done():
                    if self._completion.result().outcome == "failed":
                        raise RuntimeError("Run failed")
                    return
                else:
                    self._changed.clear()
                    await self._changed.wait()
        finally:
            self._reading = False
