import asyncio

import pytest


def test_execution_identity_defaults_to_empty_values():
    from voidx.runtime.execution_context import current_execution_identity

    identity = current_execution_identity()

    assert identity.thread_id == ""
    assert identity.session_id == ""


def test_bind_execution_identity_is_nested_and_resets():
    from voidx.runtime.execution_context import (
        ExecutionIdentity,
        bind_execution_identity,
        current_execution_identity,
    )

    outer = ExecutionIdentity(thread_id="outer-thread", session_id="outer-session")
    inner = ExecutionIdentity(thread_id="inner-thread", session_id="inner-session")

    with bind_execution_identity(outer):
        assert current_execution_identity() == outer
        with bind_execution_identity(inner):
            assert current_execution_identity() == inner
        assert current_execution_identity() == outer

    assert current_execution_identity() == ExecutionIdentity()


@pytest.mark.asyncio
async def test_bind_execution_identity_isolated_between_concurrent_tasks():
    from voidx.runtime.execution_context import (
        ExecutionIdentity,
        bind_execution_identity,
        current_execution_identity,
    )

    ready = asyncio.Barrier(2)

    async def observe(thread_id: str) -> str:
        identity = ExecutionIdentity(thread_id=thread_id)
        with bind_execution_identity(identity):
            await ready.wait()
            await asyncio.sleep(0)
            return current_execution_identity().thread_id

    assert await asyncio.gather(observe("first"), observe("second")) == ["first", "second"]
