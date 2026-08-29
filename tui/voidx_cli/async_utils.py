from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar


_T = TypeVar("_T")


async def await_cancellation_safe(
    awaitable: Awaitable[_T],
) -> tuple[_T, asyncio.CancelledError | None]:
    task = asyncio.ensure_future(awaitable)
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            return await asyncio.shield(task), cancellation
        except asyncio.CancelledError as exc:
            current = asyncio.current_task()
            if current is None or current.cancelling() == 0:
                raise
            if cancellation is None:
                cancellation = exc
            while current.cancelling():
                current.uncancel()
