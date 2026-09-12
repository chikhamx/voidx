import asyncio
import io
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from voidx_cli.terminal_trace import TerminalTrace
from voidx_cli.terminal_writer import TerminalWriter, FrameBatch


def test_disabled_and_explicit_opt_in(monkeypatch):
    monkeypatch.delenv('VOIDX_TUI_TRACE', raising=False)
    trace = TerminalTrace()
    trace.record('secret', text='screen')
    assert trace.snapshot()['events'] == []
    monkeypatch.setenv('VOIDX_TUI_TRACE', 'true')
    assert not TerminalTrace().enabled
    monkeypatch.setenv('VOIDX_TUI_TRACE', '1')
    assert TerminalTrace().enabled


def test_byte_limit_oversize_and_drop_range():
    trace = TerminalTrace(enabled=True, max_bytes=512)
    for _ in range(20):
        trace.record('write', text='中' * 30)
    trace.record('huge', text='x' * 2000)
    snap = trace.snapshot()
    assert snap['bytes'] <= 512
    assert snap['dropped_range'][0] == 1
    assert snap['dropped_range'][1] == 21
    trace.record('after')
    assert trace.snapshot()['events'][-1]['seq'] == 22


def test_concurrent_sequence_and_monotonic_time():
    trace = TerminalTrace(enabled=True)
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda n: trace.record('event', n=n), range(1000)))
    events = trace.snapshot()['events']
    assert [e['seq'] for e in events] == list(range(1, 1001))
    assert [e['monotonic_ns'] for e in events] == sorted(e['monotonic_ns'] for e in events)


def test_export_frozen_private_unique(tmp_path):
    trace = TerminalTrace(enabled=True)
    trace.record('write', text='sensitive')
    frozen = trace.snapshot()
    trace.record('later')
    a = trace.export(tmp_path, frozen)
    b = trace.export(tmp_path, frozen)
    assert a != b
    assert a.parent == tmp_path / '.voidx' / 'diagnostics'
    assert a.stat().st_mode & 0o777 == 0o600
    data = json.loads(a.read_text())
    assert len(data['events']) == 1
    assert data['metadata']['contains_sensitive_screen_text']
    assert data['metadata']['arbitrary_window_replay_guaranteed'] is False
    assert data['metadata']['pid'] > 0
    assert TerminalTrace(enabled=False).export(tmp_path) is None


def test_partial_write_and_flush(monkeypatch):
    monkeypatch.setenv('VOIDX_TUI_TRACE', '1')
    class Partial(io.StringIO):
        def write(self, value):
            return super().write(value[:2])
    stream = Partial()
    writer = TerminalWriter(stream)
    writer.write('中abc界')
    writer.flush()
    events = writer.trace.snapshot()['events']
    assert ''.join(e['text'] for e in events if e['kind'] == 'write') == stream.getvalue() == '中abc界'
    assert any(e['kind'] == 'flush' and e['ok'] for e in events)


@pytest.mark.asyncio
async def test_worker_order_drop_and_ack(monkeypatch):
    monkeypatch.setenv('VOIDX_TUI_TRACE', '1')
    writer = TerminalWriter(io.StringIO())
    writer.start(loop=asyncio.get_running_loop(), on_frame_result=lambda r: None, on_error=lambda e: None)
    with writer._condition:
        writer.submit_frame(FrameBatch(1, 1, ('old',), ''))
        writer.submit_frame(FrameBatch(2, 1, ('new',), ''))
    await writer.drain_async()
    await writer.shutdown_async()
    events = writer.trace.snapshot()['events']
    assert any(e['kind'] == 'drop' and e['generation'] == 1 for e in events)
    assert any(e['kind'] == 'execute' and e['generation'] == 2 for e in events)
    assert any(e['kind'] == 'ack' for e in events)
    assert any(e['kind'] == 'write' and e['order'] == 2 for e in events)


@pytest.mark.asyncio
@pytest.mark.parametrize('sequence', [b'\x1b[24;5~', b'\x1b[57387;5u'])
async def test_hotkey_freezes_and_exports_without_render(tmp_path, monkeypatch, sequence):
    from tui_helpers import _tui
    monkeypatch.setenv('VOIDX_TUI_TRACE', '1')
    tui = _tui(tmp_path)
    tui._terminal_writer.trace.record('before')
    assert tui._process_input(sequence) is False
    tui._terminal_writer.trace.record('after')
    await tui._finish_terminal_trace()
    files = list((tmp_path / '.voidx' / 'diagnostics').glob('*.json'))
    snapshots = [json.loads(p.read_text()) for p in files]
    assert any([e['kind'] for e in s['events']] == ['before'] for s in snapshots)
    assert any('after' in [e['kind'] for e in s['events']] for s in snapshots)


def test_export_error_uses_internal_log(tmp_path, monkeypatch):
    import voidx_cli.terminal_trace as module
    errors = []
    monkeypatch.setattr(module, 'log_internal_error', lambda exc, **kw: errors.append((exc, kw)))
    (tmp_path / '.voidx').write_text('not a directory')
    assert TerminalTrace(enabled=True).export(tmp_path) is None
    assert errors[0][1]['context'] == 'terminal_trace_export'


def test_flush_failure_is_recorded(monkeypatch):
    monkeypatch.setenv('VOIDX_TUI_TRACE', '1')
    class Broken(io.StringIO):
        def flush(self):
            raise OSError('flush failed')
    writer = TerminalWriter(Broken())
    with pytest.raises(OSError, match='flush failed'):
        writer.flush()
    assert writer.trace.snapshot()['events'][-1]['ok'] is False


@pytest.mark.asyncio
async def test_worker_commit_barrier_correlation(monkeypatch):
    monkeypatch.setenv('VOIDX_TUI_TRACE', '1')
    writer = TerminalWriter(io.StringIO())
    writer.start(loop=asyncio.get_running_loop(), on_frame_result=lambda r: None, on_error=lambda e: None)
    token = writer.submit_commit(clear_start_row=1, ansi='committed\n')
    await writer.wait(token)
    await writer.shutdown_async()
    events = writer.trace.snapshot()['events']
    for kind in ('enqueue', 'execute', 'complete', 'ack'):
        assert any(e['kind'] == kind and e['order'] == token.order for e in events)
    assert any(e['kind'] == 'enqueue' and e['barrier'] == 'shutdown' for e in events)


@pytest.mark.asyncio
async def test_disabled_hotkey_creates_no_files(tmp_path, monkeypatch):
    from tui_helpers import _tui
    monkeypatch.delenv('VOIDX_TUI_TRACE', raising=False)
    tui = _tui(tmp_path)
    assert tui._process_input(b'\x1b[24;5~') is False
    await tui._finish_terminal_trace()
    assert not (tmp_path / '.voidx' / 'diagnostics').exists()
