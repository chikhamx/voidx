"""Opt-in, byte-bounded terminal diagnostics; never writes to the terminal."""

from __future__ import annotations

import json
import os
import platform
import shutil
import threading
import time
import uuid
from collections import deque
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from voidx.observability import log_internal_error


class TerminalTrace:
    MAX_BYTES = 8 * 1024 * 1024

    def __init__(self, *, enabled: bool | None = None, max_bytes: int = MAX_BYTES):
        if max_bytes < 1:
            raise ValueError('max_bytes must be positive')
        self.enabled = os.environ.get('VOIDX_TUI_TRACE') == '1' if enabled is None else enabled
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._events: deque[bytes] = deque()
        self._bytes = 0
        self._sequence = 0
        self._dropped_end = 0
        self._metadata = {}
        if self.enabled:
            versions = {}
            for package in ('voidx', 'voidx-cli', 'rich'):
                try:
                    versions[package] = version(package)
                except PackageNotFoundError:
                    versions[package] = 'not-installed'
            self._metadata = {
                'pid': os.getpid(), 'python': platform.python_version(),
                'platform': platform.platform(), 'versions': versions,
                'terminal_size': list(shutil.get_terminal_size()),
                'environment': {key: os.environ.get(key) for key in (
                    'TERM', 'TERM_PROGRAM', 'TERM_PROGRAM_VERSION', 'COLORTERM',
                    'LANG', 'LC_CTYPE',
                )},
                'contains_sensitive_screen_text': True,
                'arbitrary_window_replay_guaranteed': False,
                'window_may_be_truncated': True,
                'write_unit': 'TextIO character prefixes, UTF-8 encoded in trace; not physical TTY bytes',
            }

    def record(self, kind: str, **fields) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._sequence += 1
            event = json.dumps(dict(fields, kind=kind, seq=self._sequence,
                                    monotonic_ns=time.monotonic_ns()), ensure_ascii=True).encode('utf-8')
            # Evict a contiguous prefix, including an oversized event, so the
            # discarded sequence range remains exact rather than hiding holes.
            while self._events and self._bytes + len(event) > self.max_bytes:
                old = self._events.popleft()
                self._bytes -= len(old)
                self._dropped_end = json.loads(old)['seq']
            if len(event) > self.max_bytes:
                self._dropped_end = self._sequence
                return
            self._events.append(event)
            self._bytes += len(event)

    def snapshot(self) -> dict:
        with self._lock:
            events = tuple(self._events)
            size, sequence, dropped = self._bytes, self._sequence, self._dropped_end
        return {
            'schema_version': 1, 'metadata': dict(self._metadata),
            'byte_limit': self.max_bytes, 'bytes': size, 'last_sequence': sequence,
            'dropped_range': [1, dropped] if dropped else None,
            'events': [json.loads(event) for event in events],
        }

    def export(self, workspace: Path, snapshot: dict | None = None) -> Path | None:
        if not self.enabled:
            return None
        try:
            frozen = self.snapshot() if snapshot is None else snapshot
            directory = Path(workspace) / '.voidx' / 'diagnostics'
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f'terminal-{os.getpid()}-{uuid.uuid4().hex}.json'
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as output:
                json.dump(frozen, output, ensure_ascii=True)
            return path
        except Exception as exc:
            log_internal_error(exc, context='terminal_trace_export')
            return None


class TerminalTraceMixin:
    """Application glue, kept separate from rendering and input semantics."""

    def _request_terminal_trace(self) -> None:
        import asyncio

        trace = self._terminal_writer.trace
        if not trace.enabled:
            return
        previous = getattr(self, '_terminal_trace_task', None)
        if previous is not None and not previous.done():
            return  # At most one frozen manual snapshot is retained during IO.
        snapshot = trace.snapshot()
        self._terminal_trace_task = asyncio.create_task(
            asyncio.to_thread(trace.export, Path(getattr(self.status, 'workspace', '.')), snapshot)
        )

    async def _finish_terminal_trace(self) -> None:
        import asyncio

        trace = getattr(self._terminal_writer, 'trace', None)
        if trace is None or not trace.enabled:
            return
        previous = getattr(self, '_terminal_trace_task', None)
        if previous is not None:
            await previous
        snapshot = trace.snapshot()
        await asyncio.to_thread(trace.export, Path(getattr(self.status, 'workspace', '.')), snapshot)

    def _trace_geometry(self, kind: str, **fields) -> None:
        trace = getattr(self._terminal_writer, 'trace', None)
        if trace is None or not trace.enabled:
            return
        trace.record(kind, **dict({
            'visible_rows': self._visible_committed_rows,
            'frame_start': self._last_frame_start_row,
            'frame_rows': self._last_frame_rows,
            'bottom_start': self._last_bottom_start_row,
            'bottom_rows': self._last_bottom_rows,
            'scroll_epoch': self._scroll_epoch,
            'generation': self._layout_generation,
            'pending_tokens': [token.order for token in self._render_state.pending_commit_tokens],
            'pending_generations': list(self._pending_layout_snapshots),
        }, **fields))
