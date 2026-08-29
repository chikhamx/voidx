"""Ordered terminal output with synchronous compatibility and a TTY worker."""

from __future__ import annotations

import asyncio
import errno
import sys
import threading
from collections import deque
from dataclasses import dataclass
from tempfile import SpooledTemporaryFile
from typing import Callable, Iterator, Literal, TextIO

from .async_utils import await_cancellation_safe


BarrierKind = Literal[
    "startup",
    "clear",
    "scroll",
    "resize",
    "drain",
    "restore",
    "shutdown",
]


@dataclass(frozen=True)
class FrameBatch:
    generation: int
    start_row: int
    target_lines: tuple[str, ...]
    cursor_ansi: str
    render_ms: float = 0.0
    force_full: bool = False


@dataclass(frozen=True)
class FrameResult:
    generation: int
    total_lines: int
    changed_lines: int
    render_ms: float
    strategy: str
    applied: bool


@dataclass(frozen=True)
class BatchToken:
    order: int
    _writer_id: int
    _future: asyncio.Future[None]


@dataclass(frozen=True)
class _BarrierBatch:
    kind: BarrierKind
    ansi: str
    invalidate_frame: bool


class _CommitPayload:
    def __init__(
        self,
        *,
        text: str | None = None,
        spool: TextIO | None = None,
        byte_length: int,
        memory_bytes: int,
    ) -> None:
        self.text = text
        self.spool = spool
        self.byte_length = byte_length
        self.memory_bytes = memory_bytes
        self.closed = False

    def parts(self, char_limit: int) -> Iterator[str]:
        if self.text is not None:
            yield self.text
            return
        if self.spool is None:
            return
        self.spool.seek(0)
        while True:
            value = self.spool.read(char_limit)
            if not value:
                return
            yield value

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.spool is not None:
            self.spool.close()


@dataclass(frozen=True)
class _CommitBatch:
    clear_start_row: int
    payload: _CommitPayload


@dataclass(frozen=True)
class _QueueEntry:
    order: int
    batch: FrameBatch | _BarrierBatch | _CommitBatch
    token: BatchToken | None


class TerminalWriter:
    """Write terminal output synchronously or through one ordered worker thread."""

    DEFAULT_BYTE_BUDGET = 64 * 1024
    DEFAULT_COMMIT_MEMORY_SOFT_LIMIT = 4 * 1024 * 1024
    MAX_ZERO_PROGRESS_WRITES = 3
    MAX_UTF8_CHAR_BYTES = 4

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        byte_budget: int = DEFAULT_BYTE_BUDGET,
        commit_memory_soft_limit: int = DEFAULT_COMMIT_MEMORY_SOFT_LIMIT,
    ) -> None:
        if byte_budget < 1:
            raise ValueError("byte_budget must be at least 1")
        if commit_memory_soft_limit < 1:
            raise ValueError("commit_memory_soft_limit must be at least 1")
        self._stream = stream
        self.byte_budget = byte_budget
        self.max_pending_bytes = max(byte_budget, self.MAX_UTF8_CHAR_BYTES)
        self.commit_memory_soft_limit = commit_memory_soft_limit
        self._pending: list[str] = []
        self._pending_bytes = 0
        self._zero_progress_writes = 0
        self.chunks_written = 0

        self._condition = threading.Condition()
        self._queue: deque[_QueueEntry] = deque()
        self._next_order = 1
        self._started = False
        self._accepting = False
        self._worker_thread: threading.Thread | None = None
        self._worker_target: TextIO | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._on_frame_result: Callable[[FrameResult], None] | None = None
        self._on_error: Callable[[Exception], None] | None = None
        self._worker_error: Exception | None = None
        self._shutdown_token: BatchToken | None = None
        self._applied_generation = 0
        self._applied_start_row = 1
        self._applied_lines: tuple[str, ...] = ()
        self._baseline_valid = False
        self._pending_commit_bytes = 0
        self._spooled_commits = 0
        self._spooled_bytes = 0

    @property
    def pending_bytes(self) -> int:
        return self._pending_bytes

    @property
    def worker_alive(self) -> bool:
        thread = self._worker_thread
        return bool(thread is not None and thread.is_alive())

    @property
    def worker_mode(self) -> bool:
        return self._started

    @property
    def pending_commit_bytes(self) -> int:
        with self._condition:
            return self._pending_commit_bytes

    @property
    def spooled_commits(self) -> int:
        with self._condition:
            return self._spooled_commits

    @property
    def spooled_bytes(self) -> int:
        with self._condition:
            return self._spooled_bytes

    def _target(self) -> TextIO:
        return self._stream if self._stream is not None else sys.stdout

    @staticmethod
    def _split_prefix(value: str, max_bytes: int) -> tuple[str, str]:
        """Return the largest character-aligned prefix within ``max_bytes``."""
        if not value or max_bytes <= 0:
            return "", value
        size = 0
        split_at = 0
        for index, char in enumerate(value):
            char_size = len(char.encode("utf-8"))
            if size + char_size > max_bytes:
                break
            size += char_size
            split_at = index + 1
        if split_at == 0:
            return "", value
        return value[:split_at], value[split_at:]

    def _require_sync_mode(self) -> None:
        if self._started:
            raise RuntimeError("synchronous terminal writes are disabled after start()")

    def write(self, value: str) -> int:
        self._require_sync_mode()
        if not value:
            return 0

        remaining = value
        while remaining:
            capacity = self.max_pending_bytes - self._pending_bytes
            if capacity <= 0:
                self.drain()
                continue

            prefix, remaining_after_prefix = self._split_prefix(remaining, capacity)
            if not prefix:
                if self._pending:
                    self.drain()
                    continue
                prefix, remaining_after_prefix = remaining[0], remaining[1:]

            self._pending.append(prefix)
            self._pending_bytes += len(prefix.encode("utf-8"))
            remaining = remaining_after_prefix
            if self._pending_bytes >= self.byte_budget:
                while self._pending and self._pending_bytes >= self.byte_budget:
                    self.drain()
        return len(value)

    def _take_chunk(self) -> str:
        if not self._pending:
            return ""
        remaining = self.byte_budget
        parts: list[str] = []
        while self._pending and remaining > 0:
            value = self._pending[0]
            encoded_size = len(value.encode("utf-8"))
            if encoded_size <= remaining:
                parts.append(value)
                self._pending.pop(0)
                self._pending_bytes -= encoded_size
                remaining -= encoded_size
                continue

            prefix, suffix = self._split_prefix(value, remaining)
            if prefix:
                prefix_size = len(prefix.encode("utf-8"))
                parts.append(prefix)
                self._pending[0] = suffix
                self._pending_bytes -= prefix_size
                remaining -= prefix_size
                continue

            char = value[0]
            char_size = len(char.encode("utf-8"))
            parts.append(char)
            if value[1:]:
                self._pending[0] = value[1:]
            else:
                self._pending.pop(0)
            self._pending_bytes -= char_size
            break
        return "".join(parts)

    def _write_some(
        self,
        target: TextIO,
        value: str,
        zero_progress: int,
    ) -> tuple[int, int]:
        written = target.write(value)
        if written is None:
            written = len(value)
        if written < 0 or written > len(value):
            raise ValueError("terminal stream returned an invalid write count")
        self.chunks_written += 1
        next_zero_progress = zero_progress + 1 if written == 0 else 0
        if next_zero_progress >= self.MAX_ZERO_PROGRESS_WRITES:
            raise BlockingIOError(errno.EAGAIN, "terminal stream made no progress")
        return written, next_zero_progress

    def drain(self) -> None:
        """Write at most one byte-budget batch in synchronous mode."""
        self._require_sync_mode()
        chunk = self._take_chunk()
        if not chunk:
            return
        try:
            written, self._zero_progress_writes = self._write_some(
                self._target(),
                chunk,
                self._zero_progress_writes,
            )
        except Exception:
            self._pending.insert(0, chunk)
            self._pending_bytes += len(chunk.encode("utf-8"))
            raise
        if written < len(chunk):
            remainder = chunk[written:]
            self._pending.insert(0, remainder)
            self._pending_bytes += len(remainder.encode("utf-8"))

    def flush(self) -> None:
        self._require_sync_mode()
        while self._pending:
            self.drain()
        flush = getattr(self._target(), "flush", None)
        if callable(flush):
            flush()

    def start(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        on_frame_result: Callable[[FrameResult], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        with self._condition:
            if self._started:
                raise RuntimeError("terminal writer already started")
            self._worker_target = self._target()
            self._loop = loop
            self._on_frame_result = on_frame_result
            self._on_error = on_error
            self._started = True
            self._accepting = True
            thread = threading.Thread(
                target=self._worker_main,
                name="voidx-terminal-writer",
                daemon=True,
            )
            self._worker_thread = thread
        try:
            thread.start()
        except Exception:
            with self._condition:
                self._started = False
                self._accepting = False
                self._worker_thread = None
                self._worker_target = None
                self._loop = None
                self._on_frame_result = None
                self._on_error = None
            raise

    def _check_submit_locked(self) -> None:
        if not self._started:
            raise RuntimeError("terminal writer is not started")
        if not self._accepting:
            if self._worker_error is not None:
                raise RuntimeError("terminal writer failed") from self._worker_error
            raise RuntimeError("terminal writer is shutting down")

    def _next_order_locked(self) -> int:
        order = self._next_order
        self._next_order += 1
        return order

    def _new_entry_locked(
        self,
        batch: _BarrierBatch | _CommitBatch,
    ) -> _QueueEntry:
        loop = self._loop
        if loop is None:
            raise RuntimeError("terminal writer is not started")
        order = self._next_order_locked()
        token = BatchToken(
            order=order,
            _writer_id=id(self),
            _future=loop.create_future(),
        )
        return _QueueEntry(order=order, batch=batch, token=token)

    def submit_frame(self, batch: FrameBatch) -> None:
        if batch.generation < 1:
            raise ValueError("frame generation must be positive")
        if batch.start_row < 1:
            raise ValueError("frame start_row must be positive")
        target_lines = tuple(batch.target_lines)
        if not all(isinstance(line, str) for line in target_lines):
            raise TypeError("frame target_lines must contain only strings")
        snapshot = FrameBatch(
            generation=batch.generation,
            start_row=batch.start_row,
            target_lines=target_lines,
            cursor_ansi=batch.cursor_ansi,
            render_ms=batch.render_ms,
            force_full=batch.force_full,
        )
        with self._condition:
            self._check_submit_locked()
            entry = _QueueEntry(
                order=self._next_order_locked(),
                batch=snapshot,
                token=None,
            )
            if self._queue and isinstance(self._queue[-1].batch, FrameBatch):
                self._queue[-1] = entry
            else:
                self._queue.append(entry)
            self._condition.notify()

    def _drop_pending_frame_locked(self) -> None:
        if self._queue and isinstance(self._queue[-1].batch, FrameBatch):
            self._queue.pop()

    def submit_barrier(
        self,
        *,
        kind: BarrierKind,
        ansi: str = "",
        invalidate_frame: bool = False,
    ) -> BatchToken:
        if kind == "shutdown":
            raise ValueError("use shutdown_async() for the shutdown barrier")
        if kind == "drain" and invalidate_frame:
            raise ValueError("drain barrier cannot invalidate the frame")
        invalidates = invalidate_frame or kind in {"clear", "scroll", "resize"}
        with self._condition:
            self._check_submit_locked()
            if invalidates:
                self._drop_pending_frame_locked()
            entry = self._new_entry_locked(
                _BarrierBatch(
                    kind=kind,
                    ansi=ansi,
                    invalidate_frame=invalidates,
                )
            )
            self._queue.append(entry)
            self._condition.notify()
            assert entry.token is not None
            return entry.token

    def _enqueue_commit_locked(
        self,
        *,
        clear_start_row: int,
        payload: _CommitPayload,
    ) -> BatchToken:
        self._drop_pending_frame_locked()
        entry = self._new_entry_locked(
            _CommitBatch(clear_start_row=clear_start_row, payload=payload)
        )
        self._queue.append(entry)
        self._condition.notify()
        assert entry.token is not None
        return entry.token

    def _spooled_payload(self, ansi: str, byte_length: int) -> _CommitPayload:
        spool = SpooledTemporaryFile(
            max_size=self.commit_memory_soft_limit,
            mode="w+t",
            encoding="utf-8",
            newline="",
        )
        try:
            spool.write(ansi)
            spool.rollover()
            spool.seek(0)
        except Exception:
            spool.close()
            raise
        return _CommitPayload(
            spool=spool,
            byte_length=byte_length,
            memory_bytes=0,
        )

    def submit_commit(self, *, clear_start_row: int, ansi: str) -> BatchToken:
        if clear_start_row < 0:
            raise ValueError("commit clear_start_row cannot be negative")
        byte_length = len(ansi.encode("utf-8"))
        with self._condition:
            self._check_submit_locked()
            if self._pending_commit_bytes + byte_length <= self.commit_memory_soft_limit:
                payload = _CommitPayload(
                    text=ansi,
                    byte_length=byte_length,
                    memory_bytes=byte_length,
                )
                self._pending_commit_bytes += byte_length
                try:
                    return self._enqueue_commit_locked(
                        clear_start_row=clear_start_row,
                        payload=payload,
                    )
                except Exception:
                    self._pending_commit_bytes -= byte_length
                    payload.close()
                    raise

        payload = self._spooled_payload(ansi, byte_length)
        try:
            with self._condition:
                self._check_submit_locked()
                self._spooled_commits += 1
                self._spooled_bytes += byte_length
                return self._enqueue_commit_locked(
                    clear_start_row=clear_start_row,
                    payload=payload,
                )
        except Exception:
            payload.close()
            raise

    async def wait(self, token: BatchToken) -> None:
        if token._writer_id != id(self):
            raise ValueError("batch token belongs to another terminal writer")
        await asyncio.shield(token._future)

    async def drain_async(self) -> None:
        token = self.submit_barrier(kind="drain")
        await self.wait(token)

    async def shutdown_async(self) -> None:
        with self._condition:
            if not self._started:
                return
            thread = self._worker_thread
            if self._shutdown_token is None and self._worker_error is None:
                self._accepting = False
                entry = self._new_entry_locked(
                    _BarrierBatch(
                        kind="shutdown",
                        ansi="",
                        invalidate_frame=False,
                    )
                )
                self._shutdown_token = entry.token
                self._queue.append(entry)
                self._condition.notify()
            token = self._shutdown_token

        async def reap() -> tuple[BaseException | None, BaseException | None]:
            wait_error: BaseException | None = None
            if token is not None:
                try:
                    await self.wait(token)
                except BaseException as exc:
                    wait_error = exc

            join_error: BaseException | None = None
            if thread is not None and thread.is_alive():
                try:
                    await asyncio.to_thread(thread.join)
                except BaseException as exc:
                    join_error = exc
            return wait_error, join_error

        (wait_error, join_error), cancellation = await await_cancellation_safe(reap())
        if wait_error is not None:
            raise wait_error
        if join_error is not None:
            raise join_error
        if cancellation is not None:
            raise cancellation

    def _worker_main(self) -> None:
        while True:
            with self._condition:
                while not self._queue:
                    self._condition.wait()
                entry = self._queue.popleft()
            try:
                should_stop = self._process_entry(entry)
            except Exception as exc:
                self._complete_token(entry.token, exc)
                self._handle_worker_error(exc)
                return
            self._complete_token(entry.token)
            if should_stop:
                return

    def _process_entry(self, entry: _QueueEntry) -> bool:
        batch = entry.batch
        if isinstance(batch, FrameBatch):
            self._process_frame(batch)
            return False
        if isinstance(batch, _CommitBatch):
            self._process_commit(batch)
            return False

        if batch.ansi:
            self._worker_write(batch.ansi)
        self._worker_flush()
        if batch.invalidate_frame:
            self._baseline_valid = False
        return batch.kind == "shutdown"

    def _process_commit(self, batch: _CommitBatch) -> None:
        try:
            clear_start_row = (
                self._applied_start_row
                if self._baseline_valid
                else batch.clear_start_row
            )
            if clear_start_row > 0:
                self._worker_write(f"\x1b[{clear_start_row};1H")
                self._worker_write("\x1b[J")
            for value in batch.payload.parts(max(1, self.byte_budget)):
                self._worker_write(value)
            self._worker_flush()
            self._baseline_valid = False
        except Exception:
            try:
                self._release_commit_payload(batch.payload)
            except Exception:
                pass
            raise
        self._release_commit_payload(batch.payload)

    def _release_commit_payload(self, payload: _CommitPayload) -> None:
        if payload.closed:
            return
        try:
            payload.close()
        finally:
            if payload.memory_bytes:
                with self._condition:
                    self._pending_commit_bytes -= payload.memory_bytes

    def _process_frame(self, batch: FrameBatch) -> None:
        if batch.generation <= self._applied_generation:
            self._publish_frame_result(
                FrameResult(
                    generation=batch.generation,
                    total_lines=len(batch.target_lines),
                    changed_lines=0,
                    render_ms=batch.render_ms,
                    strategy="stale",
                    applied=False,
                )
            )
            return

        if (
            self._baseline_valid
            and not batch.force_full
            and self._applied_start_row == batch.start_row
        ):
            changed_lines, strategy = self._write_frame_diff(
                batch.start_row,
                self._applied_lines,
                batch.target_lines,
            )
        else:
            changed_lines, strategy = self._write_frame_full(
                batch.start_row,
                batch.target_lines,
            )
        if batch.cursor_ansi:
            self._worker_write(batch.cursor_ansi)
        self._worker_flush()

        self._applied_generation = batch.generation
        self._applied_start_row = batch.start_row
        self._applied_lines = batch.target_lines
        self._baseline_valid = True
        self._publish_frame_result(
            FrameResult(
                generation=batch.generation,
                total_lines=len(batch.target_lines),
                changed_lines=changed_lines,
                render_ms=batch.render_ms,
                strategy=strategy,
                applied=True,
            )
        )

    def _write_frame_full(
        self,
        start_row: int,
        lines: tuple[str, ...],
    ) -> tuple[int, str]:
        self._worker_write(f"\x1b[{start_row};1H")
        self._worker_write("\x1b[J")
        self._worker_write("\n".join(lines))
        return len(lines), "full"

    def _write_frame_diff(
        self,
        start_row: int,
        previous: tuple[str, ...],
        current: tuple[str, ...],
    ) -> tuple[int, str]:
        total = max(len(previous), len(current))
        changed = [
            index
            for index in range(total)
            if index >= len(previous)
            or index >= len(current)
            or previous[index] != current[index]
        ]
        if total and len(changed) / total > 0.8:
            return self._write_frame_full(start_row, current)

        wrote_tail_clear = False
        for index in changed:
            self._worker_write(f"\x1b[{start_row + index};1H")
            if index >= len(current):
                self._worker_write("\x1b[J")
                wrote_tail_clear = True
                break
            self._worker_write("\x1b[K")
            self._worker_write(current[index])
        strategy = "diff-tail-clear" if wrote_tail_clear else "diff"
        return len(changed), strategy

    def _worker_stream(self) -> TextIO:
        target = self._worker_target
        if target is None:
            raise RuntimeError("terminal writer worker has no target")
        return target

    def _worker_write(self, value: str) -> None:
        target = self._worker_stream()
        remaining = value
        zero_progress = 0
        while remaining:
            chunk, suffix = self._split_prefix(remaining, self.byte_budget)
            if not chunk:
                chunk, suffix = remaining[0], remaining[1:]
            while chunk:
                written, zero_progress = self._write_some(
                    target,
                    chunk,
                    zero_progress,
                )
                chunk = chunk[written:]
            remaining = suffix

    def _worker_flush(self) -> None:
        flush = getattr(self._worker_stream(), "flush", None)
        if callable(flush):
            flush()

    def _call_soon_threadsafe(self, callback: Callable, *args) -> None:
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(callback, *args)
        except RuntimeError:
            pass

    @staticmethod
    def _finish_token(token: BatchToken, error: Exception | None) -> None:
        if token._future.done():
            return
        if error is None:
            token._future.set_result(None)
        else:
            token._future.set_exception(error)

    def _complete_token(
        self,
        token: BatchToken | None,
        error: Exception | None = None,
    ) -> None:
        if token is None:
            return
        self._call_soon_threadsafe(self._finish_token, token, error)

    def _publish_frame_result(self, result: FrameResult) -> None:
        callback = self._on_frame_result
        if callback is not None:
            self._call_soon_threadsafe(callback, result)

    def _handle_worker_error(self, error: Exception) -> None:
        with self._condition:
            if self._worker_error is not None:
                return
            self._worker_error = error
            self._accepting = False
            entries = list(self._queue)
            self._queue.clear()
        for entry in entries:
            if isinstance(entry.batch, _CommitBatch):
                try:
                    self._release_commit_payload(entry.batch.payload)
                except Exception:
                    pass
            self._complete_token(entry.token, error)
        callback = self._on_error
        if callback is not None:
            self._call_soon_threadsafe(callback, error)


__all__ = ["BatchToken", "TerminalWriter"]
