from __future__ import annotations

import asyncio
import errno
import io
import os
import select
import sys
import tempfile
import threading
import time

import pytest

import voidx_cli.terminal_writer as terminal_writer_module
from voidx_cli.terminal_writer import TerminalWriter


class _ZeroProgressStream:
    def __init__(self) -> None:
        self.calls = 0

    def write(self, value: str) -> int:
        del value
        self.calls += 1
        return 0


class _ShortWriteStream:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.value = ""

    def write(self, value: str) -> int:
        written = min(self.limit, len(value))
        self.value += value[:written]
        return written

    def flush(self) -> None:
        pass


def test_writer_flush_raises_after_bounded_zero_progress():
    stream = _ZeroProgressStream()
    writer = TerminalWriter(stream, byte_budget=4)

    with pytest.raises(BlockingIOError):
        writer.write("abcd")

    assert stream.calls == writer.MAX_ZERO_PROGRESS_WRITES
    assert writer.pending_bytes == 4


def test_writer_does_not_buffer_an_unbounded_value_when_stream_is_blocked():
    stream = _ZeroProgressStream()
    writer = TerminalWriter(stream, byte_budget=4)

    with pytest.raises(BlockingIOError):
        writer.write("abcdefghijk")

    assert writer.pending_bytes <= writer.max_pending_bytes
    assert stream.calls == writer.MAX_ZERO_PROGRESS_WRITES


def test_writer_flushes_partial_writes_without_losing_order():
    stream = _ShortWriteStream(limit=2)
    writer = TerminalWriter(stream, byte_budget=4)

    assert writer.write("abcdefgh") == 8
    writer.flush()

    assert stream.value == "abcdefgh"
    assert writer.pending_bytes == 0


def test_writer_handles_multibyte_character_larger_than_budget():
    stream = io.StringIO()
    writer = TerminalWriter(stream, byte_budget=1)

    assert writer.write("😀") == 1
    writer.flush()

    assert stream.getvalue() == "😀"
    assert writer.pending_bytes == 0


def test_writer_rejects_invalid_byte_budget():
    with pytest.raises(ValueError):
        TerminalWriter(byte_budget=0)


class _ThreadRecordingStream:
    def __init__(self) -> None:
        self.value = ""
        self.write_threads: list[int] = []
        self.flush_threads: list[int] = []
        self._lock = threading.Lock()

    def write(self, value: str) -> int:
        with self._lock:
            self.write_threads.append(threading.get_ident())
            self.value += value
        return len(value)

    def flush(self) -> None:
        with self._lock:
            self.flush_threads.append(threading.get_ident())


class _FdTextStream:
    def __init__(self, fd: int) -> None:
        self.fd = fd

    def write(self, value: str) -> int:
        return os.write(self.fd, value.encode("ascii"))

    def flush(self) -> None:
        pass


@pytest.mark.asyncio
async def test_worker_owns_stream_io_and_disables_sync_writes():
    stream = _ThreadRecordingStream()
    errors: list[Exception] = []
    writer = TerminalWriter(stream, byte_budget=4)
    caller_thread = threading.get_ident()

    writer.start(
        loop=asyncio.get_running_loop(),
        on_frame_result=lambda _result: None,
        on_error=errors.append,
    )
    token = writer.submit_barrier(
        kind="startup",
        ansi="ready",
        invalidate_frame=True,
    )
    await asyncio.wait_for(writer.wait(token), timeout=1)

    assert stream.value == "ready"
    assert stream.write_threads
    assert stream.flush_threads
    assert all(thread_id != caller_thread for thread_id in stream.write_threads)
    assert all(thread_id != caller_thread for thread_id in stream.flush_threads)
    with pytest.raises(RuntimeError):
        writer.write("blocked")
    with pytest.raises(RuntimeError):
        writer.flush()

    await asyncio.wait_for(writer.drain_async(), timeout=1)
    await asyncio.wait_for(writer.shutdown_async(), timeout=1)
    await asyncio.wait_for(writer.shutdown_async(), timeout=1)

    assert errors == []
    assert writer.worker_alive is False


@pytest.mark.skipif(sys.platform == "win32", reason="PTY backpressure coverage is POSIX-only")
@pytest.mark.asyncio
async def test_slow_pty_writer_keeps_event_loop_heartbeat_responsive():
    import pty
    import tty

    master_fd, slave_fd = pty.openpty()
    tty.setraw(slave_fd)
    stream = _FdTextStream(slave_fd)
    writer = TerminalWriter(stream, byte_budget=4096)
    start_stall = threading.Event()
    release_reached = threading.Event()
    allow_reader_drain = threading.Event()
    output_drained = threading.Event()
    stop_reader = threading.Event()
    heartbeat_ticks: list[float] = []
    ticks_at_release: list[int] = []
    frame_results: list[object] = []
    reader_errors: list[BaseException] = []
    captured = bytearray()
    restore_ansi = "<restore-exit>"
    restore_bytes = restore_ansi.encode("ascii")

    def read_slowly() -> None:
        try:
            if not start_stall.wait(timeout=2):
                raise AssertionError("PTY stall never started")
            time.sleep(0.250)
            ticks_at_release.append(len(heartbeat_ticks))
            release_reached.set()
            if not allow_reader_drain.wait(timeout=2):
                raise AssertionError("PTY reader drain was not released")
            while not stop_reader.is_set():
                readable, _, _ = select.select([master_fd], [], [], 0.05)
                if not readable:
                    continue
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError as exc:
                    if exc.errno in {errno.EIO, errno.EBADF}:
                        return
                    raise
                if not chunk:
                    return
                captured.extend(chunk)
                if captured.endswith(restore_bytes):
                    output_drained.set()
        except BaseException as exc:
            reader_errors.append(exc)
            release_reached.set()
            output_drained.set()

    reader = threading.Thread(target=read_slowly, name="slow-pty-reader", daemon=True)
    reader.start()
    heartbeat_stop = asyncio.Event()

    async def heartbeat() -> None:
        while not heartbeat_stop.is_set():
            heartbeat_ticks.append(asyncio.get_running_loop().time())
            await asyncio.sleep(0.005)

    heartbeat_task = asyncio.create_task(heartbeat())
    writer_started = False
    try:
        while len(heartbeat_ticks) < 2:
            await asyncio.sleep(0.005)
        ticks_before_stall = len(heartbeat_ticks)
        writer.start(
            loop=asyncio.get_running_loop(),
            on_frame_result=frame_results.append,
            on_error=lambda exc: reader_errors.append(exc),
        )
        writer_started = True
        start_stall.set()
        slow_payload_size = 1024 * 1024
        slow_commit = writer.submit_commit(
            clear_start_row=0,
            ansi="p" * slow_payload_size,
        )
        await asyncio.wait_for(
            asyncio.to_thread(release_reached.wait, 1),
            timeout=2,
        )

        assert reader_errors == []
        assert ticks_at_release
        assert ticks_at_release[0] - ticks_before_stall >= 10
        stall_ticks = heartbeat_ticks[ticks_before_stall : ticks_at_release[0] + 1]
        assert len(stall_ticks) >= 2
        assert max(b - a for a, b in zip(stall_ticks, stall_ticks[1:])) < 0.150

        frame_type = terminal_writer_module.FrameBatch
        writer.submit_frame(
            frame_type(
                generation=1,
                start_row=2,
                target_lines=("one-a", "one-b"),
                cursor_ansi="<cursor-1>",
            )
        )
        writer.submit_frame(
            frame_type(
                generation=2,
                start_row=2,
                target_lines=("two-a", "two-b"),
                cursor_ansi="<cursor-2>",
            )
        )
        writer.submit_frame(
            frame_type(
                generation=3,
                start_row=2,
                target_lines=("final-a", "final-b"),
                cursor_ansi="<cursor-3>",
            )
        )
        frame_drain = writer.submit_barrier(kind="drain")
        marker = writer.submit_commit(clear_start_row=0, ansi="<commit-marker>")
        restore = writer.submit_barrier(kind="restore", ansi=restore_ansi)
        allow_reader_drain.set()

        for token in (slow_commit, frame_drain, marker, restore):
            await asyncio.wait_for(writer.wait(token), timeout=5)
        await asyncio.wait_for(writer.shutdown_async(), timeout=2)
        await asyncio.wait_for(
            asyncio.to_thread(output_drained.wait, 1),
            timeout=2,
        )
        await asyncio.sleep(0)

        raw = bytes(captured)
        expected_frame = b"\x1b[2;1H\x1b[Jfinal-a\nfinal-b<cursor-3>"
        expected_marker = b"\x1b[2;1H\x1b[J<commit-marker>"
        assert reader_errors == []
        assert raw.count(b"p") == slow_payload_size
        assert raw.count(b"<commit-marker>") == 1
        assert raw.index(expected_frame) < raw.index(b"<commit-marker>")
        assert raw.endswith(expected_frame + expected_marker + restore_bytes)
        assert [result.generation for result in frame_results] == [3]
        assert frame_results[0].strategy == "full"
    finally:
        allow_reader_drain.set()
        if writer_started:
            await asyncio.wait_for(writer.shutdown_async(), timeout=2)
        heartbeat_stop.set()
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        stop_reader.set()
        for fd in (slave_fd, master_fd):
            try:
                os.close(fd)
            except OSError:
                pass
        await asyncio.to_thread(reader.join, 1)

    assert reader.is_alive() is False


class _GateStream(_ThreadRecordingStream):
    def __init__(self) -> None:
        super().__init__()
        self.block_on = ""
        self.blocked = threading.Event()
        self.release = threading.Event()

    def write(self, value: str) -> int:
        if self.block_on and self.block_on in value:
            self.blocked.set()
            if not self.release.wait(timeout=2):
                raise TimeoutError("gated terminal stream was not released")
        return super().write(value)


@pytest.mark.asyncio
async def test_frame_generation_coalesces_against_last_applied_baseline():
    stream = _GateStream()
    results: list[object] = []
    result_event = asyncio.Event()

    def on_frame_result(result: object) -> None:
        results.append(result)
        result_event.set()

    writer = TerminalWriter(stream, byte_budget=64)
    writer.start(
        loop=asyncio.get_running_loop(),
        on_frame_result=on_frame_result,
        on_error=lambda exc: pytest.fail(f"unexpected writer error: {exc}"),
    )
    try:
        frame_type = terminal_writer_module.FrameBatch
        writer.submit_frame(
            frame_type(
                generation=1,
                start_row=2,
                target_lines=("a", "b", "c", "d", "e"),
                cursor_ansi="<cursor-1>",
                render_ms=1.25,
            )
        )
        await asyncio.wait_for(result_event.wait(), timeout=1)
        result_event.clear()

        stream.block_on = "<gate>"
        gate = writer.submit_barrier(
            kind="startup",
            ansi="<gate>",
            invalidate_frame=False,
        )
        await asyncio.wait_for(asyncio.to_thread(stream.blocked.wait, 1), timeout=2)

        writer.submit_frame(
            frame_type(
                generation=2,
                start_row=2,
                target_lines=("a", "B", "c", "d", "e"),
                cursor_ansi="<cursor-2>",
                render_ms=2.25,
            )
        )
        writer.submit_frame(
            frame_type(
                generation=3,
                start_row=2,
                target_lines=("a", "b", "c", "D", "e"),
                cursor_ansi="<cursor-3>",
                render_ms=3.25,
            )
        )
        stream.release.set()
        await asyncio.wait_for(writer.wait(gate), timeout=1)
        await asyncio.wait_for(result_event.wait(), timeout=1)
        await asyncio.wait_for(writer.drain_async(), timeout=1)

        assert [result.generation for result in results] == [1, 3]
        assert results[0].strategy == "full"
        assert results[0].changed_lines == 5
        assert results[0].render_ms == 1.25
        assert results[1].strategy == "diff"
        assert results[1].changed_lines == 1
        assert results[1].render_ms == 3.25
        assert "<cursor-1>" in stream.value
        assert "<cursor-2>" not in stream.value
        assert "<cursor-3>" in stream.value
        assert "\x1b[5;1H\x1b[KD" in stream.value
    finally:
        stream.release.set()
        await asyncio.wait_for(writer.shutdown_async(), timeout=1)


@pytest.mark.asyncio
async def test_frame_stale_and_force_full_results_reflect_actual_writes():
    stream = _ThreadRecordingStream()
    results: list[object] = []
    writer = TerminalWriter(stream)
    writer.start(
        loop=asyncio.get_running_loop(),
        on_frame_result=results.append,
        on_error=lambda exc: pytest.fail(f"unexpected writer error: {exc}"),
    )
    frame_type = terminal_writer_module.FrameBatch
    try:
        writer.submit_frame(
            frame_type(
                generation=2,
                start_row=1,
                target_lines=("a", "b", "c", "d", "e"),
                cursor_ansi="",
            )
        )
        await asyncio.wait_for(writer.drain_async(), timeout=1)
        writer.submit_frame(
            frame_type(
                generation=1,
                start_row=1,
                target_lines=("stale",),
                cursor_ansi="",
            )
        )
        await asyncio.wait_for(writer.drain_async(), timeout=1)
        writer.submit_frame(
            frame_type(
                generation=3,
                start_row=1,
                target_lines=("a", "b", "c", "D", "e"),
                cursor_ansi="",
                force_full=True,
            )
        )
        await asyncio.wait_for(writer.drain_async(), timeout=1)
        await asyncio.sleep(0)

        assert [(result.generation, result.applied, result.strategy) for result in results] == [
            (2, True, "full"),
            (1, False, "stale"),
            (3, True, "full"),
        ]
    finally:
        await asyncio.wait_for(writer.shutdown_async(), timeout=1)


@pytest.mark.asyncio
async def test_commit_and_invalidating_barrier_drop_pending_frame_and_force_full():
    stream = _GateStream()
    results: list[object] = []
    result_event = asyncio.Event()

    def on_frame_result(result: object) -> None:
        results.append(result)
        result_event.set()

    writer = TerminalWriter(stream, byte_budget=64)
    writer.start(
        loop=asyncio.get_running_loop(),
        on_frame_result=on_frame_result,
        on_error=lambda exc: pytest.fail(f"unexpected writer error: {exc}"),
    )
    frame_type = terminal_writer_module.FrameBatch
    try:
        writer.submit_frame(
            frame_type(
                generation=1,
                start_row=2,
                target_lines=("a", "b", "c", "d", "e"),
                cursor_ansi="<cursor-1>",
            )
        )
        await asyncio.wait_for(result_event.wait(), timeout=1)
        result_event.clear()

        stream.block_on = "<gate>"
        gate = writer.submit_barrier(kind="startup", ansi="<gate>")
        await asyncio.wait_for(asyncio.to_thread(stream.blocked.wait, 1), timeout=2)

        writer.submit_frame(
            frame_type(
                generation=2,
                start_row=2,
                target_lines=("a", "B", "c", "d", "e"),
                cursor_ansi="<cursor-2>",
            )
        )
        commit = writer.submit_commit(clear_start_row=2, ansi="<commit>")
        writer.submit_frame(
            frame_type(
                generation=3,
                start_row=2,
                target_lines=("a", "b", "c", "D", "e"),
                cursor_ansi="<cursor-3>",
            )
        )
        stream.release.set()
        await asyncio.wait_for(writer.wait(gate), timeout=1)
        await asyncio.wait_for(writer.wait(commit), timeout=1)
        await asyncio.wait_for(writer.drain_async(), timeout=1)
        await asyncio.sleep(0)

        assert [result.generation for result in results] == [1, 3]
        assert results[-1].strategy == "full"
        assert "<cursor-2>" not in stream.value
        assert stream.value.index("<commit>") < stream.value.index("<cursor-3>")

        invalidated = writer.submit_barrier(
            kind="resize",
            invalidate_frame=True,
        )
        writer.submit_frame(
            frame_type(
                generation=4,
                start_row=2,
                target_lines=("a", "b", "c", "d", "E"),
                cursor_ansi="<cursor-4>",
            )
        )
        await asyncio.wait_for(writer.wait(invalidated), timeout=1)
        await asyncio.wait_for(writer.drain_async(), timeout=1)
        await asyncio.sleep(0)

        assert results[-1].generation == 4
        assert results[-1].strategy == "full"
    finally:
        stream.release.set()
        await asyncio.wait_for(writer.shutdown_async(), timeout=1)


class _TrackingSpool:
    def __init__(self, *args, **kwargs) -> None:
        self._inner = tempfile.SpooledTemporaryFile(*args, **kwargs)
        self.rollover_calls = 0
        self.close_calls = 0

    def write(self, value: str) -> int:
        return self._inner.write(value)

    def read(self, size: int = -1) -> str:
        return self._inner.read(size)

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._inner.seek(offset, whence)

    def rollover(self) -> None:
        self.rollover_calls += 1
        self._inner.rollover()

    def close(self) -> None:
        self.close_calls += 1
        self._inner.close()


@pytest.mark.asyncio
async def test_aggregate_commit_soft_limit_forces_spool_and_closes_it(monkeypatch):
    created: list[_TrackingSpool] = []

    def spool_factory(*args, **kwargs):
        spool = _TrackingSpool(*args, **kwargs)
        created.append(spool)
        return spool

    monkeypatch.setattr(
        terminal_writer_module,
        "SpooledTemporaryFile",
        spool_factory,
        raising=False,
    )
    stream = _GateStream()
    writer = TerminalWriter(
        stream,
        byte_budget=64,
        commit_memory_soft_limit=8,
    )
    writer.start(
        loop=asyncio.get_running_loop(),
        on_frame_result=lambda _result: None,
        on_error=lambda exc: pytest.fail(f"unexpected writer error: {exc}"),
    )
    try:
        stream.block_on = "<gate>"
        gate = writer.submit_barrier(kind="startup", ansi="<gate>")
        await asyncio.wait_for(asyncio.to_thread(stream.blocked.wait, 1), timeout=2)

        first = writer.submit_commit(clear_start_row=0, ansi="first1")
        second = writer.submit_commit(clear_start_row=0, ansi="second")

        assert writer.pending_commit_bytes == 6
        assert writer.spooled_commits == 1
        assert writer.spooled_bytes == 6
        assert len(created) == 1
        assert created[0].rollover_calls == 1

        stream.release.set()
        await asyncio.wait_for(writer.wait(gate), timeout=1)
        await asyncio.wait_for(writer.wait(first), timeout=1)
        await asyncio.wait_for(writer.wait(second), timeout=1)
        await asyncio.wait_for(writer.drain_async(), timeout=1)

        assert stream.value.index("first1") < stream.value.index("second")
        assert writer.pending_commit_bytes == 0
        assert created[0].close_calls == 1
    finally:
        stream.release.set()
        await asyncio.wait_for(writer.shutdown_async(), timeout=1)


class _BrokenPipeGateStream:
    def __init__(self) -> None:
        self.blocked = threading.Event()
        self.release = threading.Event()

    def write(self, value: str) -> int:
        del value
        self.blocked.set()
        if not self.release.wait(timeout=2):
            raise TimeoutError("broken-pipe stream was not released")
        raise BrokenPipeError(errno.EPIPE, "terminal closed")

    def flush(self) -> None:
        pass


@pytest.mark.asyncio
async def test_worker_epipe_fails_all_tokens_once_and_rejects_new_batches():
    stream = _BrokenPipeGateStream()
    errors: list[Exception] = []
    error_event = asyncio.Event()

    def on_error(exc: Exception) -> None:
        errors.append(exc)
        error_event.set()

    writer = TerminalWriter(stream)
    writer.start(
        loop=asyncio.get_running_loop(),
        on_frame_result=lambda _result: None,
        on_error=on_error,
    )
    first = writer.submit_barrier(kind="startup", ansi="boom")
    await asyncio.wait_for(asyncio.to_thread(stream.blocked.wait, 1), timeout=2)
    second = writer.submit_commit(clear_start_row=0, ansi="queued")
    third = writer.submit_barrier(kind="drain")
    stream.release.set()

    for token in (first, second, third):
        with pytest.raises(BrokenPipeError):
            await asyncio.wait_for(writer.wait(token), timeout=1)
    await asyncio.wait_for(error_event.wait(), timeout=1)
    await asyncio.sleep(0)

    assert len(errors) == 1
    assert isinstance(errors[0], BrokenPipeError)
    with pytest.raises(RuntimeError):
        writer.submit_barrier(kind="drain")
    await asyncio.wait_for(writer.shutdown_async(), timeout=1)
    assert writer.worker_alive is False


@pytest.mark.asyncio
async def test_worker_partial_writes_preserve_commit_order():
    stream = _ShortWriteStream(limit=2)
    writer = TerminalWriter(stream, byte_budget=4)
    writer.start(
        loop=asyncio.get_running_loop(),
        on_frame_result=lambda _result: None,
        on_error=lambda exc: pytest.fail(f"unexpected writer error: {exc}"),
    )
    try:
        first = writer.submit_commit(clear_start_row=0, ansi="abcdefgh")
        second = writer.submit_commit(clear_start_row=0, ansi="ijklmnop")
        await asyncio.wait_for(writer.wait(first), timeout=1)
        await asyncio.wait_for(writer.wait(second), timeout=1)
        assert stream.value == "abcdefghijklmnop"
    finally:
        await asyncio.wait_for(writer.shutdown_async(), timeout=1)


@pytest.mark.asyncio
async def test_drain_waits_for_prior_commit_without_waiting_for_later_batch():
    stream = _GateStream()
    stream.block_on = "<later>"
    writer = TerminalWriter(stream, byte_budget=64)
    writer.start(
        loop=asyncio.get_running_loop(),
        on_frame_result=lambda _result: None,
        on_error=lambda exc: pytest.fail(f"unexpected writer error: {exc}"),
    )
    later_wait: asyncio.Task[None] | None = None
    try:
        first = writer.submit_commit(clear_start_row=0, ansi="<first>")
        drain_task = asyncio.create_task(writer.drain_async())
        await asyncio.sleep(0)
        later = writer.submit_commit(clear_start_row=0, ansi="<later>")
        later_wait = asyncio.create_task(writer.wait(later))

        await asyncio.wait_for(asyncio.to_thread(stream.blocked.wait, 1), timeout=2)
        await asyncio.wait_for(drain_task, timeout=1)
        await asyncio.wait_for(writer.wait(first), timeout=1)

        assert later_wait.done() is False
        assert "<first>" in stream.value
        assert "<later>" not in stream.value

        stream.release.set()
        await asyncio.wait_for(later_wait, timeout=1)
        assert stream.value.index("<first>") < stream.value.index("<later>")
    finally:
        stream.release.set()
        if later_wait is not None:
            await asyncio.gather(later_wait, return_exceptions=True)
        await asyncio.wait_for(writer.shutdown_async(), timeout=1)


class _GateZeroProgressStream:
    def __init__(self) -> None:
        self.calls = 0
        self.blocked = threading.Event()
        self.release = threading.Event()

    def write(self, value: str) -> int:
        del value
        self.blocked.set()
        if not self.release.wait(timeout=2):
            raise TimeoutError("zero-progress stream was not released")
        self.calls += 1
        return 0

    def flush(self) -> None:
        pass


@pytest.mark.asyncio
async def test_worker_zero_progress_reports_one_error_and_fails_all_tokens():
    stream = _GateZeroProgressStream()
    errors: list[Exception] = []
    error_event = asyncio.Event()

    def on_error(exc: Exception) -> None:
        errors.append(exc)
        error_event.set()

    writer = TerminalWriter(stream, byte_budget=4)
    writer.start(
        loop=asyncio.get_running_loop(),
        on_frame_result=lambda _result: None,
        on_error=on_error,
    )
    first = writer.submit_barrier(kind="startup", ansi="blocked")
    await asyncio.wait_for(asyncio.to_thread(stream.blocked.wait, 1), timeout=2)
    second = writer.submit_barrier(kind="drain")
    stream.release.set()

    for token in (first, second):
        with pytest.raises(BlockingIOError):
            await asyncio.wait_for(writer.wait(token), timeout=1)
    await asyncio.wait_for(error_event.wait(), timeout=1)
    await asyncio.sleep(0)

    assert stream.calls == writer.MAX_ZERO_PROGRESS_WRITES
    assert len(errors) == 1
    assert isinstance(errors[0], BlockingIOError)
    await asyncio.wait_for(writer.shutdown_async(), timeout=1)


@pytest.mark.asyncio
async def test_worker_error_closes_queued_spool_payload(monkeypatch):
    created: list[_TrackingSpool] = []

    def spool_factory(*args, **kwargs):
        spool = _TrackingSpool(*args, **kwargs)
        created.append(spool)
        return spool

    monkeypatch.setattr(
        terminal_writer_module,
        "SpooledTemporaryFile",
        spool_factory,
    )
    stream = _BrokenPipeGateStream()
    errors: list[Exception] = []
    error_event = asyncio.Event()

    def on_error(exc: Exception) -> None:
        errors.append(exc)
        error_event.set()

    writer = TerminalWriter(stream, commit_memory_soft_limit=4)
    writer.start(
        loop=asyncio.get_running_loop(),
        on_frame_result=lambda _result: None,
        on_error=on_error,
    )
    try:
        failing = writer.submit_barrier(kind="startup", ansi="boom")
        await asyncio.wait_for(asyncio.to_thread(stream.blocked.wait, 1), timeout=2)
        queued = writer.submit_commit(clear_start_row=0, ansi="spooled")
        drain = writer.submit_barrier(kind="drain")

        assert len(created) == 1
        assert created[0].rollover_calls == 1
        assert created[0].close_calls == 0

        stream.release.set()
        for token in (failing, queued, drain):
            with pytest.raises(BrokenPipeError):
                await asyncio.wait_for(writer.wait(token), timeout=1)
        await asyncio.wait_for(error_event.wait(), timeout=1)
        await asyncio.sleep(0)

        assert len(errors) == 1
        assert created[0].close_calls == 1
    finally:
        stream.release.set()
        await asyncio.wait_for(writer.shutdown_async(), timeout=1)
        for spool in created:
            if spool.close_calls == 0:
                spool.close()


@pytest.mark.asyncio
async def test_commit_clear_prefers_last_applied_frame_start_row():
    stream = _ThreadRecordingStream()
    frame_result = asyncio.Event()
    writer = TerminalWriter(stream)
    writer.start(
        loop=asyncio.get_running_loop(),
        on_frame_result=lambda _result: frame_result.set(),
        on_error=lambda exc: pytest.fail(f"unexpected writer error: {exc}"),
    )
    try:
        writer.submit_frame(
            terminal_writer_module.FrameBatch(
                generation=1,
                start_row=7,
                target_lines=("frame",),
                cursor_ansi="",
            )
        )
        await asyncio.wait_for(frame_result.wait(), timeout=1)

        commit = writer.submit_commit(clear_start_row=2, ansi="<commit>")
        await asyncio.wait_for(writer.wait(commit), timeout=1)

        before_commit = stream.value[: stream.value.index("<commit>")]
        assert before_commit.count("\x1b[7;1H") == 2
        assert "\x1b[2;1H" not in before_commit
    finally:
        await asyncio.wait_for(writer.shutdown_async(), timeout=1)


@pytest.mark.asyncio
async def test_cancelled_wait_does_not_cancel_batch_completion():
    stream = _GateStream()
    stream.block_on = "<gate>"
    writer = TerminalWriter(stream, byte_budget=64)
    writer.start(
        loop=asyncio.get_running_loop(),
        on_frame_result=lambda _result: None,
        on_error=lambda exc: pytest.fail(f"unexpected writer error: {exc}"),
    )
    try:
        token = writer.submit_barrier(kind="startup", ansi="<gate>")
        await asyncio.wait_for(asyncio.to_thread(stream.blocked.wait, 1), timeout=2)
        cancelled_wait = asyncio.create_task(writer.wait(token))
        await asyncio.sleep(0)
        cancelled_wait.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_wait

        stream.release.set()
        await asyncio.wait_for(writer.wait(token), timeout=1)
    finally:
        stream.release.set()
        await asyncio.wait_for(writer.shutdown_async(), timeout=1)


@pytest.mark.parametrize("kind", ["clear", "scroll", "resize"])
@pytest.mark.asyncio
async def test_barrier_kind_drops_pending_frame_and_invalidates_baseline(kind):
    stream = _GateStream()
    results: list[object] = []
    result_event = asyncio.Event()

    def on_frame_result(result: object) -> None:
        results.append(result)
        result_event.set()

    writer = TerminalWriter(stream, byte_budget=64)
    writer.start(
        loop=asyncio.get_running_loop(),
        on_frame_result=on_frame_result,
        on_error=lambda exc: pytest.fail(f"unexpected writer error: {exc}"),
    )
    frame_type = terminal_writer_module.FrameBatch
    try:
        writer.submit_frame(
            frame_type(
                generation=1,
                start_row=2,
                target_lines=("a", "b", "c", "d", "e"),
                cursor_ansi="",
            )
        )
        await asyncio.wait_for(result_event.wait(), timeout=1)
        result_event.clear()

        stream.block_on = "<gate>"
        gate = writer.submit_barrier(kind="startup", ansi="<gate>")
        await asyncio.wait_for(asyncio.to_thread(stream.blocked.wait, 1), timeout=2)
        writer.submit_frame(
            frame_type(
                generation=2,
                start_row=2,
                target_lines=("a", "B", "c", "d", "e"),
                cursor_ansi="",
            )
        )
        barrier = writer.submit_barrier(kind=kind)
        writer.submit_frame(
            frame_type(
                generation=3,
                start_row=2,
                target_lines=("a", "b", "c", "D", "e"),
                cursor_ansi="",
            )
        )

        stream.release.set()
        await asyncio.wait_for(writer.wait(gate), timeout=1)
        await asyncio.wait_for(writer.wait(barrier), timeout=1)
        await asyncio.wait_for(writer.drain_async(), timeout=1)
        await asyncio.sleep(0)

        assert [result.generation for result in results] == [1, 3]
        assert results[-1].strategy == "full"
    finally:
        stream.release.set()
        await asyncio.wait_for(writer.shutdown_async(), timeout=1)


@pytest.mark.asyncio
async def test_drain_barrier_rejects_frame_invalidation():
    writer = TerminalWriter(_ThreadRecordingStream())
    writer.start(
        loop=asyncio.get_running_loop(),
        on_frame_result=lambda _result: None,
        on_error=lambda exc: pytest.fail(f"unexpected writer error: {exc}"),
    )
    try:
        with pytest.raises(ValueError, match="drain barrier cannot invalidate"):
            writer.submit_barrier(kind="drain", invalidate_frame=True)
    finally:
        await asyncio.wait_for(writer.shutdown_async(), timeout=1)


@pytest.mark.asyncio
async def test_submit_frame_snapshots_mutable_target_lines():
    stream = _GateStream()
    results: list[object] = []
    writer = TerminalWriter(stream, byte_budget=64)
    writer.start(
        loop=asyncio.get_running_loop(),
        on_frame_result=results.append,
        on_error=lambda exc: pytest.fail(f"unexpected writer error: {exc}"),
    )
    try:
        stream.block_on = "<gate>"
        gate = writer.submit_barrier(kind="startup", ansi="<gate>")
        await asyncio.wait_for(asyncio.to_thread(stream.blocked.wait, 1), timeout=2)

        mutable_lines = ["original"]
        writer.submit_frame(
            terminal_writer_module.FrameBatch(
                generation=1,
                start_row=2,
                target_lines=mutable_lines,
                cursor_ansi="",
            )
        )
        mutable_lines[0] = "mutated"
        mutable_lines.append("late")

        stream.release.set()
        await asyncio.wait_for(writer.wait(gate), timeout=1)
        await asyncio.wait_for(writer.drain_async(), timeout=1)
        await asyncio.sleep(0)

        assert [result.total_lines for result in results] == [1]
        assert "original" in stream.value
        assert "mutated" not in stream.value
        assert "late" not in stream.value
    finally:
        stream.release.set()
        await asyncio.wait_for(writer.shutdown_async(), timeout=1)


@pytest.mark.asyncio
async def test_frame_start_row_change_forces_full_render():
    stream = _ThreadRecordingStream()
    results: list[object] = []
    writer = TerminalWriter(stream)
    writer.start(
        loop=asyncio.get_running_loop(),
        on_frame_result=results.append,
        on_error=lambda exc: pytest.fail(f"unexpected writer error: {exc}"),
    )
    try:
        writer.submit_frame(
            terminal_writer_module.FrameBatch(
                generation=1,
                start_row=2,
                target_lines=("a", "b", "c", "d", "e"),
                cursor_ansi="",
            )
        )
        await asyncio.wait_for(writer.drain_async(), timeout=1)
        writer.submit_frame(
            terminal_writer_module.FrameBatch(
                generation=2,
                start_row=4,
                target_lines=("a", "b", "c", "D", "e"),
                cursor_ansi="",
            )
        )
        await asyncio.wait_for(writer.drain_async(), timeout=1)
        await asyncio.sleep(0)

        assert [result.strategy for result in results] == ["full", "full"]
        assert stream.value.endswith("\x1b[4;1H\x1b[Ja\nb\nc\nD\ne")
    finally:
        await asyncio.wait_for(writer.shutdown_async(), timeout=1)


@pytest.mark.asyncio
async def test_coalesced_frames_equal_direct_latest_full_render():
    stream = _GateStream()
    results: list[object] = []
    writer = TerminalWriter(stream, byte_budget=64)
    writer.start(
        loop=asyncio.get_running_loop(),
        on_frame_result=results.append,
        on_error=lambda exc: pytest.fail(f"unexpected writer error: {exc}"),
    )
    try:
        stream.block_on = "<gate>"
        gate = writer.submit_barrier(kind="startup", ansi="<gate>")
        await asyncio.wait_for(asyncio.to_thread(stream.blocked.wait, 1), timeout=2)

        frame_type = terminal_writer_module.FrameBatch
        writer.submit_frame(
            frame_type(
                generation=1,
                start_row=3,
                target_lines=("one-a", "one-b"),
                cursor_ansi="<cursor-1>",
            )
        )
        writer.submit_frame(
            frame_type(
                generation=2,
                start_row=3,
                target_lines=("two-a", "two-b"),
                cursor_ansi="<cursor-2>",
            )
        )
        writer.submit_frame(
            frame_type(
                generation=3,
                start_row=3,
                target_lines=("final-a", "final-b"),
                cursor_ansi="<cursor-3>",
            )
        )

        stream.release.set()
        await asyncio.wait_for(writer.wait(gate), timeout=1)
        await asyncio.wait_for(writer.drain_async(), timeout=1)
        await asyncio.sleep(0)

        assert stream.value == (
            "<gate>"
            "\x1b[3;1H"
            "\x1b[J"
            "final-a\nfinal-b"
            "<cursor-3>"
        )
        assert [result.generation for result in results] == [3]
        assert results[0].strategy == "full"
    finally:
        stream.release.set()
        await asyncio.wait_for(writer.shutdown_async(), timeout=1)


class _ShutdownFlushFailureStream:
    def __init__(self, failure: OSError) -> None:
        self.failure = failure
        self.flush_calls = 0
        self.value = ""

    def write(self, value: str) -> int:
        self.value += value
        return len(value)

    def flush(self) -> None:
        self.flush_calls += 1
        if self.flush_calls == 2:
            raise self.failure


@pytest.mark.asyncio
async def test_shutdown_async_reaps_worker_and_propagates_shutdown_barrier_error():
    failure = OSError("shutdown flush failed")
    stream = _ShutdownFlushFailureStream(failure)
    errors: list[Exception] = []
    writer = TerminalWriter(stream)
    writer.start(
        loop=asyncio.get_running_loop(),
        on_frame_result=lambda _result: None,
        on_error=errors.append,
    )
    startup = writer.submit_barrier(kind="startup", ansi="ready")
    await asyncio.wait_for(writer.wait(startup), timeout=1)

    with pytest.raises(OSError) as caught:
        await asyncio.wait_for(writer.shutdown_async(), timeout=1)

    assert caught.value is failure
    assert writer.worker_alive is False
    assert errors == [failure]

    with pytest.raises(OSError) as repeated:
        await asyncio.wait_for(writer.shutdown_async(), timeout=1)
    assert repeated.value is failure


class _ShutdownExitGateWriter(TerminalWriter):
    def __init__(self, stream) -> None:
        super().__init__(stream)
        self.shutdown_completion_started = threading.Event()
        self.release_worker_exit = threading.Event()

    def _complete_token(self, token, error=None) -> None:
        super()._complete_token(token, error)
        if token is self._shutdown_token and error is None:
            self.shutdown_completion_started.set()
            if not self.release_worker_exit.wait(timeout=2):
                raise TimeoutError("shutdown worker exit was not released")


@pytest.mark.asyncio
async def test_shutdown_async_cancellation_waits_until_worker_is_reaped(monkeypatch):
    writer = _ShutdownExitGateWriter(io.StringIO())
    writer.start(
        loop=asyncio.get_running_loop(),
        on_frame_result=lambda _result: None,
        on_error=lambda exc: pytest.fail(f"unexpected writer error: {exc}"),
    )
    startup = writer.submit_barrier(kind="startup", ansi="ready")
    await asyncio.wait_for(writer.wait(startup), timeout=1)
    join_started = asyncio.Event()
    original_to_thread = asyncio.to_thread

    async def tracked_to_thread(func, /, *args, **kwargs):
        if (
            getattr(func, "__self__", None) is writer._worker_thread
            and getattr(func, "__name__", "") == "join"
        ):
            join_started.set()
        return await original_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(terminal_writer_module.asyncio, "to_thread", tracked_to_thread)
    shutdown_task = asyncio.create_task(writer.shutdown_async())
    try:
        await asyncio.wait_for(join_started.wait(), timeout=1)
        shutdown_task.cancel()
        done, _ = await asyncio.wait({shutdown_task}, timeout=0.05)
        assert done == set()
        assert writer.worker_alive is True

        writer.release_worker_exit.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(shutdown_task, timeout=1)
    finally:
        writer.release_worker_exit.set()
        if not shutdown_task.done():
            shutdown_task.cancel()
            await asyncio.gather(shutdown_task, return_exceptions=True)

    assert writer.worker_alive is False
