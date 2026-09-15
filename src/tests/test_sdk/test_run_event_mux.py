"""Run-wide multiplexing budgets and real turn identities."""
import asyncio
from uuid import uuid4

from voidx.agent.application.runtime import semantic_channel
from voidx.agent.domain import semantic_events as e


def event(channel, kind="text"):
    envelope = dict(**channel.identity, event_id=uuid4(), sequence=99,
                    timestamp=0.0, agent_id=None, parent_tool_call_id=None)
    if kind == "terminal":
        return e.TurnCancelled(**envelope, payload={"reason": "cancelled"})
    return e.ToolStarted(**envelope, payload={"tool_call_id": "tool", "name": "read", "arguments": {}})


def test_channel_exposes_nonblocking_mux_consumption():
    async def run():
        channel = semantic_channel.SemanticChannel(session_id="s", thread_id="t", turn_id="u")
        assert callable(getattr(channel, "take_ready", None)), "mux requires single-event nonblocking consumption"
        await channel.publish(event(channel))
        channel._finish(event(channel, "terminal"))
        assert channel.take_ready().sequence == 1
        assert channel.take_ready().sequence == 2
        assert channel.take_ready() is None
        assert channel.drained
    asyncio.run(run())


def test_global_queue_budget_precedes_child_publication():
    async def run():
        class Budget:
            def __init__(self):
                self.tokens = asyncio.Semaphore(1)
                self.ready = asyncio.Event()
            async def acquire(self):
                await self.tokens.acquire()
            def release(self):
                self.tokens.release()
            def notify(self):
                self.ready.set()
        budget = Budget()
        a = semantic_channel.SemanticChannel(session_id="s1", thread_id="t1", turn_id="u1", budget=budget)
        b = semantic_channel.SemanticChannel(session_id="s2", thread_id="t2", turn_id="u2", budget=budget)
        await a.publish(event(a))
        producer = asyncio.create_task(b.publish(event(b)))
        await asyncio.sleep(0)
        assert not producer.done()
        assert a.take_ready().session_id == "s1"
        await producer
        assert b.take_ready().session_id == "s2"
        assert budget.tokens._value == 1
    asyncio.run(run())


def mux_type():
    from importlib.util import find_spec
    assert find_spec("voidx.agent.application.runtime.run_event_mux") is not None, "run-wide fair aggregation is missing"
    from voidx.agent.application.runtime.run_event_mux import RunEventMux
    return RunEventMux


def test_mux_round_robin_and_independent_completion():
    async def run():
        mux = mux_type()(capacity=4, turns=2)
        a = await mux.register(session_id="a", thread_id="ta")
        b = await mux.register(session_id="b", thread_id="tb")
        for channel in (a, a, b, b):
            await channel.publish(event(channel))
        a._finish(event(a, "terminal"))
        b._finish(event(b, "terminal"))
        assert not mux.completion.done()
        mux.finish("completed")
        events = [item async for item in mux.events()]
        assert [item.session_id for item in events] == ["a", "b"] * 3
        assert [item.sequence for item in events] == [1, 1, 2, 2, 3, 3]
        assert len({item.event_id for item in events}) == 6
        assert mux.registered == mux.queued == 0
        assert (await mux.completion).outcome == "completed"
    asyncio.run(run())


def test_mux_reclaims_a_thousand_turns_and_bounds_admission():
    async def run():
        mux = mux_type()(capacity=1, turns=1)
        stream = mux.events()
        for _ in range(1100):
            channel = await mux.register(session_id="real", thread_id="actual")
            channel._finish(event(channel, "terminal"))
            assert mux.registered == 1
            await anext(stream)
            assert mux.registered == 0
        mux.finish("completed")
        assert [item async for item in stream] == []
    asyncio.run(run())


def test_hot_producer_and_q1_cancelled_publish_return_tokens():
    async def run():
        mux = mux_type()(capacity=1, turns=2)
        a = await mux.register(session_id="hot", thread_id="hot-thread")
        b = await mux.register(session_id="cold", thread_id="cold-thread")
        stream = mux.events()
        await a.publish(event(a))
        pending = asyncio.create_task(b.publish(event(b)))
        await asyncio.sleep(0)
        assert mux.queued == 1 and not pending.done()
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        assert mux.queued == 1
        assert (await anext(stream)).session_id == "hot"
        assert mux.queued == 0
        await b.publish(event(b))
        assert (await anext(stream)).session_id == "cold"
        a._finish(event(a, "terminal"))
        b._finish(event(b, "terminal"))
        mux.finish("completed")
        assert len([item async for item in stream]) == 2
        assert mux.queued == mux.registered == 0
    asyncio.run(run())


def test_started_is_unique_under_concurrent_publication():
    import pytest
    async def run():
        channel = semantic_channel.SemanticChannel(session_id="s", thread_id="t", turn_id="u")
        started = e.TurnStarted(**event(channel).model_dump(exclude={"kind", "payload"}), payload={"text": "input", "metadata": {}})
        results = await asyncio.gather(channel.publish(started), channel.publish(started), return_exceptions=True)
        assert sum(isinstance(result, ValueError) for result in results) == 1
        assert channel._sequence == 1
    asyncio.run(run())


def test_continuously_hot_channel_cannot_starve_ready_peer():
    async def run():
        mux = mux_type()(turns=2, capacity=4)
        hot = await mux.register(session_id="hot", thread_id="h")
        cold = await mux.register(session_id="cold", thread_id="c")
        stream = mux.events()
        for _ in range(100):
            await hot.publish(event(hot))
            await cold.publish(event(cold))
            selected = [await anext(stream), await anext(stream)]
            assert {item.session_id for item in selected} == {"hot", "cold"}
        hot._finish(event(hot, "terminal"))
        cold._finish(event(cold, "terminal"))
        mux.finish("completed")
        assert len([item async for item in stream]) == 2
        assert mux.registered == mux.queued == 0
    asyncio.run(run())
