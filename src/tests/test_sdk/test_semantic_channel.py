"""Bounded per-run publication contracts."""
import asyncio
from uuid import uuid4

import pytest

from voidx.agent.domain import semantic_events as e
from voidx.agent.application.runtime.semantic_channel import SemanticChannel


def event(text="hello", **changes):
    return e.ToolStarted(
        event_id=uuid4(), session_id="session", thread_id="thread", turn_id="turn",
        sequence=99, agent_id=None, parent_tool_call_id=None, timestamp=0.0,
        payload={"tool_call_id": "tool", "name": "read", "arguments": {"text": [text]}},
    ).model_copy(update=changes)


def channel(**kwargs):
    return SemanticChannel(session_id="session", thread_id="thread", turn_id="turn", **kwargs)


def test_defaults_and_invalid_limits():
    async def run():
        assert channel().capacity == 256
        for value in (0, -1, True, 1.5):
            with pytest.raises(ValueError):
                channel(capacity=value)
            with pytest.raises(ValueError):
                channel(max_event_bytes=value)
    asyncio.run(run())


def test_backpressure_snapshot_and_no_put_task():
    async def run():
        c = channel(capacity=1)
        original = event()
        before = asyncio.all_tasks()
        await c.publish(original)
        assert asyncio.all_tasks() == before
        original.payload.arguments["text"].append("mutated")
        pending = asyncio.create_task(c.publish(event("second")))
        await asyncio.sleep(0)
        assert not pending.done()
        assert asyncio.all_tasks() == before | {pending}
        iterator = c.events()
        first = await anext(iterator)
        assert first.payload.arguments == {"text": ["hello"]}
        first.payload.arguments["text"].append("consumer")
        assert original.payload.arguments["text"] == ["hello", "mutated"]
        await pending
        assert (await anext(iterator)).sequence == 2
        await iterator.aclose()
    asyncio.run(run())


def test_concurrent_sequence_and_run_isolation():
    async def run():
        c, other = channel(capacity=1), channel()
        async def consume():
            iterator = c.events()
            result = [await anext(iterator) for _ in range(30)]
            await iterator.aclose()
            return result
        consumer = asyncio.create_task(consume())
        await asyncio.gather(*(c.publish(event(str(i))) for i in range(30)))
        assert [item.sequence for item in await consumer] == list(range(1, 31))
        await other.publish(event())
        iterator = other.events()
        assert (await anext(iterator)).sequence == 1
        await iterator.aclose()
        with pytest.raises(ValueError):
            await c.publish(event(turn_id="foreign"))
    asyncio.run(run())


def test_size_failure_does_not_consume_sequence():
    async def run():
        c = channel(max_event_bytes=512)
        with pytest.raises(e.EventSizeExceeded):
            await c.publish(event("secret" * 1000))
        await c.publish(event())
        iterator = c.events()
        assert (await anext(iterator)).sequence == 1
        await iterator.aclose()
    asyncio.run(run())


def test_terminal_cannot_be_published():
    async def run():
        c = channel()
        terminal = e.TurnCancelled(**event().model_dump(exclude={"kind", "payload"}), payload={"reason": "user"})
        with pytest.raises(ValueError):
            await c.publish(terminal)
    asyncio.run(run())


def test_configured_limit_can_exceed_default():
    async def run():
        c = channel(max_event_bytes=512 * 1024)
        await c.publish(event("x" * (300 * 1024)))
        iterator = c.events()
        assert len((await anext(iterator)).payload.arguments["text"][0]) == 300 * 1024
        await iterator.aclose()
    asyncio.run(run())
