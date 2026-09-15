"""SDK initialization cancellation against real headless composition."""
import asyncio

import pytest

from test_headless_resources import setup_agent
from voidx.agent.adapters.persistence.headless_locks import HeadlessFileLock
from voidx.agent.adapters.persistence.session_repository import create_session, get_session
from voidx.bootstrap import headless
from voidx.bootstrap.managed_guidance import ManagedGuidance, current_guidance
from voidx.persistence.file_lock import acquire_file_lock_sync, release_file_lock_sync


def assert_locked(session_id):
    with pytest.raises(BlockingIOError):
        acquire_file_lock_sync(HeadlessFileLock("session", session_id)._path, blocking=False)


def assert_released(session_id):
    handle = acquire_file_lock_sync(HeadlessFileLock("session", session_id)._path, blocking=False)
    release_file_lock_sync(handle)


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [True, False], ids=["explicit", "created"])
@pytest.mark.parametrize("finish", ["cancel", "close"])
async def test_wrong_identity_preserves_initialization(setup_agent, tmp_path, monkeypatch, explicit, finish):
    agent = setup_agent
    session = await create_session(workspace=str(tmp_path)) if explicit else None
    other = await create_session(workspace=str(tmp_path))
    entered = asyncio.Event()
    observed = []
    original = headless._build_execution

    async def held(*args, **kwargs):
        observed.append(await get_session(args[3].id))
        entered.set()
        await asyncio.Event().wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(headless, "_build_execution", held)
    guidance = ManagedGuidance()
    token = current_guidance.set(guidance)

    async def consume():
        try:
            return [event async for event in agent.stream(
                "RESTORE_PROBE", session_id=session.id if session else "", workspace=str(tmp_path))]
        finally:
            guidance.close()

    consumer = asyncio.create_task(consume())
    waiter = asyncio.create_task(guidance.submit("pending"))
    current_guidance.reset(token)
    try:
        async with asyncio.timeout(20):
            await entered.wait()
            actual = observed[0]
            assert actual is not None
            assert actual.workspace == str(tmp_path)
            assert_locked(actual.id)
            with pytest.raises(ValueError, match="session_id"):
                await agent.cancel(session_id=other.id)
            assert not consumer.done()
            assert not waiter.done()
            assert_locked(actual.id)
            if finish == "cancel":
                await agent.cancel(session_id=actual.id)
            else:
                await agent.aclose()
            assert await consumer == []
            with pytest.raises(RuntimeError, match="closed before delivery"):
                await waiter
            assert_released(actual.id)
    finally:
        await agent.aclose()
        await asyncio.gather(consumer, waiter, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["concurrent", "external", "cleanup_error"])
async def test_initialization_cleanup_is_joined(setup_agent, tmp_path, monkeypatch, mode):
    from voidx.agent.adapters.langgraph.execution import LangGraphExecution
    from voidx.agent.adapters.subagent.inprocess_gateway import InProcessSubagentGateway

    agent = setup_agent
    session = await create_session(workspace=str(tmp_path))
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []
    original_restore = LangGraphExecution.restore_runtime_state
    original_close = InProcessSubagentGateway.close_all

    async def restore(self):
        entered.set()
        await asyncio.Event().wait()
        await original_restore(self)

    async def close(self):
        calls.append(self)
        cleaning.set()
        await release.wait()
        await original_close(self)
        if mode == "cleanup_error":
            raise RuntimeError("initialization cleanup sentinel")

    monkeypatch.setattr(LangGraphExecution, "restore_runtime_state", restore)
    monkeypatch.setattr(InProcessSubagentGateway, "close_all", close)

    async def consume():
        return [event async for event in agent.stream("RESTORE_PROBE", session_id=session.id, workspace=str(tmp_path))]

    consumer = asyncio.create_task(consume())
    async with asyncio.timeout(20):
        await entered.wait()
        cancelling = asyncio.create_task(agent.cancel(session_id=session.id))
        await cleaning.wait()
        if mode == "concurrent":
            second = asyncio.create_task(agent.aclose())
        else:
            cancelling.cancel()
            second = None
        await asyncio.sleep(0)
        assert_locked(session.id)
        release.set()
        results = await asyncio.gather(consumer, cancelling, *([second] if second else []), return_exceptions=True)
        assert len(calls) == 1
        assert_released(session.id)
        if mode == "concurrent":
            assert results == [[], None, None]
        elif mode == "external":
            assert results[0] == []
            assert isinstance(results[1], asyncio.CancelledError)
        else:
            def leaves(error):
                if isinstance(error, BaseExceptionGroup):
                    return [leaf for child in error.exceptions for leaf in leaves(child)]
                return [error]
            for result in results:
                assert any(isinstance(e, RuntimeError) and str(e) == "initialization cleanup sentinel" for e in leaves(result))


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["wrong", "cancel", "close"])
async def test_initialization_completed_before_stream_resumes(setup_agent, tmp_path, monkeypatch, operation):
    import voidx.sdk.agent as sdk

    agent = setup_agent
    session = await create_session(workspace=str(tmp_path))
    other = await create_session(workspace=str(tmp_path))
    original = sdk.build_headless_run
    actions = []

    async def build(*args, **kwargs):
        result = await original(*args, **kwargs)
        # Queue the caller before the build task wakes its stream consumer.
        if operation == "close":
            action = agent.aclose()
        else:
            action = agent.cancel(session_id=other.id if operation == "wrong" else session.id)
        actions.append(asyncio.create_task(action))
        return result

    monkeypatch.setattr(sdk, "build_headless_run", build)
    async with asyncio.timeout(20):
        events = [event async for event in agent.stream(
            "RESTORE_PROBE", session_id=session.id, workspace=str(tmp_path))]
        results = await asyncio.gather(*actions, return_exceptions=True)
        if operation == "wrong":
            assert isinstance(results[0], ValueError)
            assert events[-1].kind == "turn.completed"
        else:
            assert results == [None]
            assert not events or events[-1].kind == "turn.cancelled"
        await agent.aclose()
        assert_released(session.id)
