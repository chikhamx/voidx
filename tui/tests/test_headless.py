"""Tests for headless mode (web gateway input without TUI)."""

import asyncio

import pytest

from voidx.ui.output.types import ThreadExecutionContext
from tui_helpers import _tui


@pytest.mark.asyncio
async def test_run_headless_consumes_queued_input(tmp_path):
    """run_headless must consume _queue and call on_submit for each item."""
    tui = _tui(tmp_path)
    received: list[str] = []

    async def on_submit(text: str) -> bool:
        received.append(text)
        return True

    headless_task = asyncio.create_task(tui.run_headless(on_submit))

    tui.submit_external_input("hello from gateway")
    await asyncio.wait_for(asyncio.sleep(0.1), timeout=1)

    tui._queue.put_nowait(None)
    await asyncio.wait_for(headless_task, timeout=1)

    assert received == ["hello from gateway"]


@pytest.mark.asyncio
async def test_run_headless_processes_multiple_inputs(tmp_path):
    """run_headless must keep consuming across multiple submissions."""
    tui = _tui(tmp_path)
    received: list[str] = []

    async def on_submit(text: str) -> bool:
        received.append(text)
        return True

    headless_task = asyncio.create_task(tui.run_headless(on_submit))

    tui.submit_external_input("first")
    tui.submit_external_input("second")
    await asyncio.wait_for(asyncio.sleep(0.15), timeout=1)

    tui._queue.put_nowait(None)
    await asyncio.wait_for(headless_task, timeout=1)

    assert received == ["first", "second"]


@pytest.mark.asyncio
async def test_run_headless_preserves_thread_id_context(tmp_path):
    tui = _tui(tmp_path)
    received: list[tuple[str, str]] = []

    async def on_submit(text: str, *, context: ThreadExecutionContext) -> bool:
        received.append((text, context.thread_id))
        return True

    headless_task = asyncio.create_task(tui.run_headless(on_submit))

    tui.submit_external_input("thread scoped", thread_id="t2")
    await asyncio.wait_for(asyncio.sleep(0.1), timeout=1)

    tui._queue.put_nowait(None)
    await asyncio.wait_for(headless_task, timeout=1)

    assert received == [("thread scoped", "t2")]
