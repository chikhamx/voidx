"""Async publishing is additive to the legacy synchronous port."""

import asyncio
import inspect

from voidx.agent.ports.events import (
    EventPublisher,
    NullSemanticEventPublisher,
    SemanticEventPublisher,
)


def test_async_port_does_not_change_legacy_contract():
    assert not inspect.iscoroutinefunction(EventPublisher.publish)
    assert inspect.iscoroutinefunction(SemanticEventPublisher.publish)


def test_null_publisher_returns_without_scheduling_or_buffering():
    async def run():
        publisher = NullSemanticEventPublisher()
        before = asyncio.all_tasks()
        # Null output deliberately ignores the value, without serialization.
        for _ in range(1024):
            await publisher.publish(None)  # type: ignore[arg-type]
        assert asyncio.all_tasks() == before
        assert not vars(publisher)

    asyncio.run(run())
