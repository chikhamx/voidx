from __future__ import annotations

import asyncio
import multiprocessing
import time
from pathlib import Path
import pytest

import voidx.persistence.jsonl as jsonl_store
from voidx.agent.adapters.persistence.session_repository import (
    create_session,
    delete_session,
    ensure_session,
    get_session,
)




def _acquire_session_lock_in_child(
    data_dir: str,
    connection: multiprocessing.connection.Connection,
) -> None:
    import voidx.persistence.sqlite as child_store

    child_store.DATA_DIR = Path(data_dir)

    async def _run() -> None:
        connection.send(("ready", 0.0))
        started = time.monotonic()
        async with jsonl_store.session_directory_locks(("session-1",)):
            connection.send(("acquired", time.monotonic() - started))

    try:
        asyncio.run(_run())
    finally:
        connection.close()

@pytest.mark.parametrize(
    "session_id",
    [
        "",
        "../escape",
        "nested/session",
        "nested\\session",
        "MixedCase",
        "session:colon",
        "session.",
        "con",
        "nul",
        "com1",
    ],
)
def test_session_dir_rejects_unsafe_storage_ids(session_id: str) -> None:
    with pytest.raises(ValueError, match="session id"):
        jsonl_store.session_dir(session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "ensure", "get", "delete"])
async def test_session_crud_rejects_unsafe_storage_ids(operation: str) -> None:
    session_id = "../escape"

    with pytest.raises(ValueError, match="session id"):
        if operation == "create":
            await create_session(session_id=session_id)
        elif operation == "ensure":
            await ensure_session(session_id, ".")
        elif operation == "get":
            await get_session(session_id)
        else:
            await delete_session(session_id)


@pytest.mark.asyncio
async def test_directory_lock_fails_closed_without_platform_file_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(jsonl_store, "fcntl", None)
    monkeypatch.setattr(jsonl_store, "msvcrt", None, raising=False)

    with pytest.raises(RuntimeError, match="cross-process session directory locking"):
        async with jsonl_store.session_directory_locks(("session-1",)):
            pass


@pytest.mark.asyncio
async def test_session_directory_lock_serializes_across_processes() -> None:
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(
        target=_acquire_session_lock_in_child,
        args=(str(jsonl_store.store.DATA_DIR), send),
    )

    async with jsonl_store.session_directory_locks(("session-1",)):
        process.start()
        send.close()
        assert await asyncio.to_thread(receive.poll, 5.0)
        assert receive.recv()[0] == "ready"
        assert not await asyncio.to_thread(receive.poll, 0.25)

    try:
        assert await asyncio.to_thread(receive.poll, 5.0)
        event, waited = receive.recv()
        assert event == "acquired"
        assert waited >= 0.20
    finally:
        receive.close()
        await asyncio.to_thread(process.join, 5.0)
        if process.is_alive():
            process.terminate()
            process.join()
    assert process.exitcode == 0
