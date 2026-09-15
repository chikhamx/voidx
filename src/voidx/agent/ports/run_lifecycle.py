"""Capacity contracts for optional run-owned producers and semantic queues."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any, ContextManager, Literal, Protocol


class EventBudget(Protocol):
    async def acquire(self) -> None: ...
    def release(self) -> None: ...
    def notify(self) -> None: ...


class RunLifecycle(Protocol):
    def record_cleanup_error(self, error: BaseException) -> None: ...

    @property
    def accepting_dispatches(self) -> bool: ...

    def reserve_dispatch(self) -> ContextManager[None]: ...

    def spawn(self, factory: Callable[[], Coroutine[Any, Any, Any]], *,
              role: Literal["execution", "renewal", "pump"] = "execution") -> asyncio.Task[Any]: ...


class InteractionBudget(Protocol):
    capacity: int
    closed: bool
    async def acquire(self) -> None: ...
    def release(self) -> None: ...


class SchedulerEvents(Protocol):
    async def committed(self, *, decision=None, goal_phase=None) -> None: ...
