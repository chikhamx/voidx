"""Tests for guidance preview cleanup when /guide is submitted during a busy turn.

When /guide is submitted via the TUI bypass path (asyncio.create_task), the
GuidanceSubmitted event sets a preview (⚡text) on the busy/vibe line. If the
guidance is not consumed by the turn (e.g. submitted after _discard runs),
GuidanceCommitted is never emitted and the preview stays stuck.

The _consume finally block must clear any residual guidance preview after the
turn ends so it doesn't linger on the vibe line.
"""

import asyncio

import pytest

from tui_helpers import *  # noqa: F403

from voidx.presentation.output.dock import dock
from voidx.presentation.output.events import DockEventConsumer, GuidanceSubmitted


@pytest.mark.asyncio
async def test_consume_finally_clears_residual_guidance_preview(tmp_path):
    """After a turn ends, any guidance preview left in the dock must be cleared
    so it doesn't stay stuck on the vibe line."""
    tui = _tui(tmp_path)
    turn_done = asyncio.Event()

    async def on_submit(text: str) -> bool:
        # Simulate: during the turn, GuidanceSubmitted set a preview.
        # But GuidanceCommitted was never emitted (timing race —
        # submit_guidance ran after _discard_pending_guidance).
        dock.set_guidance_preview("stuck guidance")
        return True

    consumer = asyncio.create_task(tui._consume(on_submit))
    try:
        tui._queue.put_nowait("hello")
        # Wait for the turn to fully complete by watching _current_submit_task
        # clear (set to None in the finally block).
        while tui._current_submit_task is not None:
            await asyncio.sleep(0.01)
        # Give the finally block one more yield to finish clearing preview.
        await asyncio.sleep(0)

        # The _consume finally block should have cleared the preview.
        assert dock._guidance_preview == "", (
            f"guidance preview not cleared after turn end: {dock._guidance_preview!r}"
        )
    finally:
        tui._queue.put_nowait(None)
        await asyncio.wait_for(consumer, timeout=2)


@pytest.mark.asyncio
async def test_consume_finally_clears_guidance_preview_set_via_event(tmp_path):
    """Same as above but the preview is set via the GuidanceSubmitted event
    through DockEventConsumer, matching the real events-mode production path."""
    tui = _tui(tmp_path)
    consumer_dock = DockEventConsumer(dock)

    async def on_submit(text: str) -> bool:
        # Simulate submit_guidance emitting GuidanceSubmitted via emit_direct,
        # which the DockEventConsumer handles by calling dock.set_guidance_preview.
        consumer_dock.handle(GuidanceSubmitted(text="stuck via event"))
        assert dock._guidance_preview == "stuck via event"
        return True

    consumer = asyncio.create_task(tui._consume(on_submit))
    try:
        tui._queue.put_nowait("hello")
        # Wait for the turn to fully complete by watching _current_submit_task
        # clear (set to None in the finally block).
        while tui._current_submit_task is not None:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0)

        assert dock._guidance_preview == "", (
            f"guidance preview not cleared after turn end: {dock._guidance_preview!r}"
        )
    finally:
        tui._queue.put_nowait(None)
        await asyncio.wait_for(consumer, timeout=2)


@pytest.mark.asyncio
async def test_consume_finally_clears_preview_on_cancelled_turn(tmp_path):
    """If the turn is cancelled, the finally block must still clear any
    residual guidance preview."""
    tui = _tui(tmp_path)

    async def on_submit(text: str) -> bool:
        dock.set_guidance_preview("stuck on cancel")
        # Simulate a long-running turn that gets cancelled
        await asyncio.Event().wait()
        return True

    consumer = asyncio.create_task(tui._consume(on_submit))
    try:
        tui._queue.put_nowait("hello")
        # Wait for the turn to start
        while tui._current_submit_task is None:
            await asyncio.sleep(0.01)
        # Cancel the turn
        tui._submit_cancel_requested = True
        tui._current_submit_task.cancel()
        # Wait for the finally block to run
        while tui._current_submit_task is not None:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0)

        assert dock._guidance_preview == "", (
            f"guidance preview not cleared after cancelled turn: {dock._guidance_preview!r}"
        )
    finally:
        tui._queue.put_nowait(None)
        await asyncio.wait_for(consumer, timeout=2)
