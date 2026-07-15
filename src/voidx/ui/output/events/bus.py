"""Async UI event bus."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any

from voidx.agent.graph.thread_context import current_thread_execution_state

from voidx.logging.tool_log import log_tool_event
from voidx.runtime.ui import UiEventTimeout
from voidx.ui.output.events.schema import UiEvent






@dataclass
class _QueuedEvent:
    event: UiEvent
    future: asyncio.Future[Any] | None = None


class UiEventBus:
    """Single-consumer async queue for all UI mutations."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[_QueuedEvent | None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._consumer: Any | None = None
        self._last_error: BaseException | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done() and self._queue is not None

    @property
    def last_error(self) -> BaseException | None:
        return self._last_error

    def clear_error(self) -> BaseException | None:
        error = self._last_error
        self._last_error = None
        return error

    def _with_current_thread_id(self, event: UiEvent) -> UiEvent:
        if getattr(event, "thread_id", ""):
            return event
        state = current_thread_execution_state()
        thread_id = str(getattr(state, "thread_id", "") or "")
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
        self._task = asyncio.create_task(self._run(), name="voidx-ui-event-bus")

    async def emit(self, event: UiEvent) -> bool:
        if not self.is_running or self._queue is None:
            return False
        await self._queue.put(_QueuedEvent(self._with_current_thread_id(event)))
        return True

    def emit_nowait(self, event: UiEvent) -> bool:
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
        while True:
            item = await self._queue.get()
            try:
                if item is None:
                    return
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
