"""Lightweight async retry with exponential backoff + jitter."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


async def retry_async(
    coro_fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int,
    base_delay: float,
    max_delay: float,
    jitter: bool,
    label: str,
    logger: logging.Logger | None = None,
    retry_on: type[Exception] | tuple[type[Exception], ...] | None = None,
) -> T:
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await coro_fn()
        except Exception as exc:
            last_exc = exc
            if retry_on is not None and not isinstance(exc, retry_on):
                raise
            if attempt >= max_attempts:
                raise
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            if jitter:
                delay *= 0.5 + random.random()
            if logger:
                logger.warning(
                    "%s attempt %d/%d failed: %s — retrying in %.2fs",
                    label, attempt, max_attempts, exc, delay,
                )
            await asyncio.sleep(delay)
    raise last_exc  # unreachable
