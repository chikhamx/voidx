"""Physical acceptance for deferred and failed transcript commits."""

import asyncio
import threading

import pytest

from test_layout_terminal_model import _assert_applied_screen, _terminal_tui
from test_terminal_writer import _FailingFrameStream
from tui_helpers import setup_dock  # noqa: F401
from voidx.presentation.output.dock import dock
from voidx_cli.terminal_writer import TerminalWriter


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True], ids=["sync", "worker"])
async def test_no_safe_region_defers_without_changing_screen_or_history(
    tmp_path, monkeypatch, worker,
):
    async with _terminal_tui(
        tmp_path, monkeypatch, worker=worker, height=3,
    ) as (tui, screen, stream, drain):
        stream.write("HISTORY-SENTINEL\nBOTTOM-1\nBOTTOM-2\nBOTTOM-3")
        assert screen.history == ("HISTORY-SENTINEL",)
        tui._has_rendered_frame = True
        tui._last_frame_start_row = 4
        tui._last_bottom_start_row = 1
        tui._last_bottom_rows = 3
        dock.start_turn("DEFERRED-QUESTION")
        dock.append_message("DEFERRED-ANSWER")
        before = (screen.rows, tuple(screen.history), screen.cursor)
        count = tui._committed_line_count
        for _ in range(2):
            assert tui._flush_committed(force=True) is None
            await drain()
            assert (screen.rows, tuple(screen.history), screen.cursor) == before
            assert tui._committed_line_count == count
            assert tui._pending_commit_tokens == []
            assert dock.consume_force_flush_request() is True


class _GatedFailureStream(_FailingFrameStream):
    def __init__(self, target):
        super().__init__()
        self.target = target
        self.blocked = threading.Event()
        self.release = threading.Event()

    def write(self, value):
        if self._mode == "original":
            self.blocked.set()
            if not self.release.wait(timeout=5):
                raise TimeoutError("failure gate was not released")
        count = super().write(value)
        self.target.write(value)
        return count


@pytest.mark.asyncio
async def test_pending_commit_write_failure_preserves_physical_state_and_retries(
    tmp_path, monkeypatch,
):
    async with _terminal_tui(
        tmp_path, monkeypatch, worker=False, height=12,
    ) as (tui, screen, stream, drain):
        stream.write("HISTORY-SENTINEL\n" * 13)
        dock.start_turn("RETRY-QUESTION")
        dock.append_message("RETRY-ANSWER")
        tui._render_frame()
        await drain()
        _assert_applied_screen(tui, screen)
        before = (screen.rows, tuple(screen.history))
        count = tui._committed_line_count
        sync_writer = tui._terminal_writer
        failing = _GatedFailureStream(stream)
        writer = TerminalWriter(failing)
        errors = []
        writer.start(
            loop=asyncio.get_running_loop(),
            on_frame_result=tui._handle_terminal_frame_result,
            on_error=errors.append,
        )
        tui._terminal_writer = writer
        try:
            dock.request_force_flush()
            failing.arm_failure()
            token = tui._flush_committed(force=True)
            assert token is not None
            assert await asyncio.to_thread(failing.blocked.wait, 2)
            assert tui._pending_commit_tokens
            tui._render_frame()
            assert (screen.rows, tuple(screen.history)) == before
            assert tui._committed_line_count == count
            tasks = tuple(tui._render_state.pending_commit_tasks.values())
            failing.release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
            await asyncio.sleep(0)
            assert errors == [failing._original_error]
            assert not writer._baseline_valid
            assert not screen.synchronized
            assert (screen.rows, tuple(screen.history)) == before
            assert tui._committed_line_count == count
            assert tui._pending_commit_tokens == []
            assert tui._pending_commit_updates == {}
            assert tui._pending_terminal_operations == {}
            assert dock.consume_force_flush_request() is True
        finally:
            failing.release.set()
            await asyncio.wait_for(writer.shutdown_async(), timeout=5)
            tui._terminal_writer = sync_writer
        tui._render_frame()
        await drain()
        tui._flush_committed(force=True)
        await drain()
        tui._render_frame()
        await drain()
        _assert_applied_screen(tui, screen)
        combined = "\n".join((*screen.history, *screen.rows))
        assert combined.count("RETRY-QUESTION") == 1
        assert combined.count("RETRY-ANSWER") == 1
        assert tuple(screen.history[:len(before[1])]) == before[1]
        assert "status" not in "\n".join(screen.history)
