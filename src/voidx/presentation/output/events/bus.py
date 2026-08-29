"""Async UI event bus."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Callable

from voidx.platform.execution_context import current_execution_identity

from voidx.observability.tool_log import log_tool_event
from voidx.agent.ports.ui import UiEventTimeout
from voidx.presentation.output.events.schema import AssistantStreamUpdated, UiEvent


@dataclass
class _QueuedEvent:
    event: UiEvent
    future: asyncio.Future[Any] | None = None


@dataclass(frozen=True)
class UiEventBusMetrics(Mapping[str, int]):
    """Point-in-time queue and delivery counters for :class:`UiEventBus`."""

    queue_depth: int
    processed: int
    coalesced: int

    def __getitem__(self, key: str) -> int:
        if key not in {"queue_depth", "processed", "coalesced"}:
            raise KeyError(key)
        return getattr(self, key)

    def __iter__(self) -> Iterator[str]:
        return iter(("queue_depth", "processed", "coalesced"))

    def __len__(self) -> int:
        return 3

    def __call__(self) -> dict[str, int]:
        """Allow both ``bus.metrics`` and legacy ``bus.metrics()`` callers."""
        return dict(self)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Mapping):
            return dict(self) == dict(other)
        return super().__eq__(other)


class UiEventBus:
    """Single-consumer async queue for all UI mutations."""

    DEFAULT_BATCH_EVENT_LIMIT = 32
    DEFAULT_BATCH_TIME_BUDGET = 0.004

    def __init__(
        self,
        *,
        batch_event_limit: int = DEFAULT_BATCH_EVENT_LIMIT,
        batch_time_budget: float = DEFAULT_BATCH_TIME_BUDGET,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if batch_event_limit < 1:
            raise ValueError("batch_event_limit must be at least 1")
        if batch_time_budget < 0:
            raise ValueError("batch_time_budget must not be negative")
        self._batch_event_limit = batch_event_limit
        self._batch_time_budget = batch_time_budget
        self._clock = clock
        self._queue: asyncio.Queue[_QueuedEvent | None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._consumer: Any | None = None
        self._last_error: BaseException | None = None
        self._processed = 0
        self._coalesced = 0

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done() and self._queue is not None

    @property
    def last_error(self) -> BaseException | None:
        return self._last_error

    @property
    def metrics(self) -> UiEventBusMetrics:
        queue_depth = self._queue.qsize() if self._queue is not None else 0
        return UiEventBusMetrics(queue_depth, self._processed, self._coalesced)

    @property
    def queue_depth(self) -> int:
        return self.metrics.queue_depth

    @property
    def processed(self) -> int:
        return self._processed

    @property
    def coalesced(self) -> int:
        return self._coalesced

    def clear_error(self) -> BaseException | None:
        error = self._last_error
        self._last_error = None
        return error

    def _with_current_thread_id(self, event: UiEvent) -> UiEvent:
        if getattr(event, "thread_id", ""):
            return event
        thread_id = current_execution_identity().thread_id
        if not thread_id:
            return event
        return event.model_copy(update={"thread_id": thread_id})

    def start(self, consumer: Any) -> None:
        if self.is_running:
            self._consumer = consumer
            return
        self._queue = asyncio.Queue()
        self._consumer = consumer
        self._last_error = None
        self._processed = 0
        self._coalesced = 0
        self._task = asyncio.create_task(self._run(), name="voidx-ui-event-bus")

    async def emit(self, event: UiEvent) -> bool:
        if not self.is_running or self._queue is None:
            return False
        await self._queue.put(_QueuedEvent(self._with_current_thread_id(event)))
        return True

    def emitnowait(self, event: UiEvent) -> bool:
        if not self.is_running or self._queue is None:
            return False
        self._queue.put_nowait(_QueuedEvent(self._with_current_thread_id(event)))
        return True

    def emit_direct(self, event: UiEvent) -> bool:
        """Apply a non-streaming event directly to the consumer, bypassing the queue.

        Use for one-shot events (tool calls, errors, etc.) that should appear
        immediately without waiting for queued streaming events to drain.
        """
        if not self.is_running or self._consumer is None:
            return False
        event = self._with_current_thread_id(event)
        if hasattr(self._consumer, "handle_direct"):
            self._consumer.handle_direct(event)
        else:
            result = self._consumer.handle(event)
            if inspect.isawaitable(result):
                asyncio.create_task(result)
        return True

    @staticmethod
    def _coalescible(event: UiEvent, future: asyncio.Future[Any] | None) -> bool:
        """Return whether an event explicitly has the cumulative stream contract.

        ``AssistantStreamUpdated`` is the existing cumulative-snapshot event in
        the Python UI track.  ``snapshot_contract`` is intentionally optional in
        the event model so old producers remain valid; a producer can opt out by
        setting it to ``"delta"``.  Requests are always barriers because their
        future is part of the event's semantics.
        """
        if future is not None or not isinstance(event, AssistantStreamUpdated):
            return False
        if not getattr(event, "stream_id", ""):
            return False
        return getattr(event, "snapshot_contract", "cumulative") == "cumulative"

    def _coalesce_ready(
        self,
        item: _QueuedEvent,
    ) -> tuple[_QueuedEvent, _QueuedEvent | None, bool]:
        """Coalesce one contiguous stream run and retain the first barrier.

        Stream phases are barriers too: thinking and answer text have separate
        rendering state and must both reach the consumer before commit.
        """
        if self._queue is None or not self._coalescible(item.event, item.future):
            return item, None, False
        latest = item
        pending: _QueuedEvent | None = None
        stop_pending = False
        stream_id = item.event.stream_id
        thread_id = getattr(item.event, "thread_id", "")
        phase = item.event.phase
        while True:
            try:
                candidate = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if (
                candidate is not None
                and self._coalescible(candidate.event, candidate.future)
                and candidate.event.thread_id == thread_id
                and candidate.event.stream_id == stream_id
                and candidate.event.phase == phase
            ):
                # This queued event is represented by ``latest`` and therefore
                # must be acknowledged without being delivered separately.
                self._queue.task_done()
                latest = candidate
                self._coalesced += 1
                continue
            if candidate is None:
                stop_pending = True
            else:
                pending = candidate
            break
        return latest, pending, stop_pending

    async def request(self, event: UiEvent, *, timeout: float = 5.0, max_retries: int = 10) -> Any:
        if not self.is_running or self._queue is None:
            raise RuntimeError("UI event bus is not running")
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        await self._queue.put(_QueuedEvent(self._with_current_thread_id(event), future))
        for attempt in range(max_retries):
            try:
                return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
            except asyncio.TimeoutError:
                if future.done():
                    return future.result()
                message = (
                    "UiEventBus.request stall: "
                    f"event={type(event).__name__} attempt={attempt + 1}/{max_retries} "
                    f"elapsed={(attempt + 1) * timeout:.1f}s"
                )
                log_tool_event(
                    "ui_event_bus_request_stall",
                    tool_name="ui_event_bus",
                    message=message,
                )
        future.cancel()
        message = f"UiEventBus.request timed out after {max_retries * timeout}s: {type(event).__name__}"
        log_tool_event(
            "ui_event_bus_request_timeout",
            tool_name="ui_event_bus",
            message=message,
        )
        raise UiEventTimeout(
            f"UiEventBus.request timed out after {max_retries * timeout}s: {type(event).__name__}"
        )

    async def drain(self) -> None:
        if self._queue is not None:
            await self._queue.join()
        if self._last_error is not None:
            raise self._last_error

    async def stop(self) -> None:
        if self._queue is None or self._task is None:
            return
        await self._queue.join()
        await self._queue.put(None)
        await self._task
        self._queue = None
        self._task = None
        self._consumer = None

    async def _run(self) -> None:
        assert self._queue is not None
        pending: _QueuedEvent | None = None
        pending_set = False
        pending_stop = False
        while True:
            if pending_set:
                item = pending
                pending = None
                pending_set = False
            else:
                item = await self._queue.get()
            if item is None:
                self._queue.task_done()
                return

            batch_started_at = self._clock()
            batch_processed = 0
            while True:
                item, pending, pending_stop = self._coalesce_ready(item)
                pending_set = pending is not None
                try:
                    result = None
                    try:
                        if self._consumer is None:
                            raise RuntimeError("UI event bus has no consumer")
                        result = self._consumer.handle(item.event)
                        if inspect.isawaitable(result):
                            result = await result
                    except BaseException as exc:
                        self._last_error = exc
                        if item.future is not None and not item.future.done():
                            item.future.set_exception(exc)
                    else:
                        if item.future is not None and not item.future.done():
                            item.future.set_result(result)
                finally:
                    self._queue.task_done()

                self._processed += 1
                batch_processed += 1
                if pending_stop:
                    # The sentinel was already removed from the queue by the
                    # coalescing look-ahead and still needs acknowledgement.
                    self._queue.task_done()
                    return
                if (
                    batch_processed >= self._batch_event_limit
                    or self._clock() - batch_started_at >= self._batch_time_budget
                ):
                    await asyncio.sleep(0)
                    break
                if pending_set:
                    item = pending
                    pending = None
                    pending_set = False
                    continue
                try:
                    item = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
