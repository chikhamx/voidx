"""Independent production WS processes sharing workspace and persistence."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile

import pytest
from websockets.asyncio.client import connect


@pytest.mark.asyncio
@pytest.mark.parametrize('direction', ['legacy-sdk', 'sdk-legacy', 'same-sdk-legacy'])
async def test_production_mixed_process_exclusion_and_release(tmp_path, direction):
    await run_mixed_process_acceptance(tmp_path, direction)


async def run_mixed_process_acceptance(tmp_path, direction, *, goal=False):
    directory = Path(tempfile.mkdtemp(prefix=f'voidx-s6-{direction}-', dir=os.environ.get('S6_EVIDENCE_DIR', '/tmp')))
    same = direction.startswith('same')
    roles = ['same'] if same else ['legacy', 'sdk']
    processes, streams, sockets = [], [], []
    serial = 0

    def events(role):
        path = directory / f'{role}.events.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    async def wait(predicate):
        async with asyncio.timeout(60):
            while not predicate():
                for process in processes:
                    assert process.returncode is None, (directory, process.returncode)
                await asyncio.sleep(.02)

    async def rpc(socket, method, params):
        nonlocal serial
        serial += 1
        await socket.send(json.dumps(dict(jsonrpc='2.0', id=serial, method=method, params=params)))
        while True:
            message = json.loads(await socket.recv())
            if message.get('id') == serial:
                assert 'error' not in message, (directory, message)
                return message['result']

    legacy_role, sdk_role = ('same', 'same') if same else ('legacy', 'sdk')
    try:
        for role in roles:
            stream = (directory / f'{role}.log').open('w')
            streams.append(stream)
            processes.append(await asyncio.create_subprocess_exec(sys.executable, '-m',
                'tests.test_sdk.mixed_process_worker', str(tmp_path), str(directory), role,
                stdout=stream, stderr=asyncio.subprocess.STDOUT,
                env={**os.environ, 'S6_GOAL_PROCESS': '1' if goal else '0'}))
            await wait(lambda: (directory / f'{role}.url').exists())
            sockets.append(await connect((directory / f'{role}.url').read_text()))
        legacy_ws, sdk_ws = sockets[0], sockets[-1]
        if not same:
            assert len({p.pid for p in processes}) == 2
        root = (await rpc(legacy_ws, 'session.create', {'profile': 'coding'}))['thread_id']
        sdk = (await rpc(sdk_ws, 'session.create', {'profile': 'coding'}))['thread_id']
        if goal:
            (directory / 'start-goal').touch()
        else:
            await rpc(legacy_ws, 'session.submit', {'thread_id': root, 'text': '/loop write process evidence'})
        await wait(lambda: any(e['event'] == 'background_ready' for e in events(legacy_role)))
        assert (tmp_path / 'legacy-prime.txt').read_text() == 'prime'
        if direction == 'legacy-sdk':
            (directory / 'allow-legacy').touch()
            if goal:
                await wait(lambda: (tmp_path / 'legacy-fresh.txt').exists())
            else:
                await wait(lambda: any(e['event'] == 'fresh_returned' for e in events(legacy_role)))
        await rpc(sdk_ws, 'session.submit', {'thread_id': sdk, 'text': 'SDK_PROCESS_WRITE'})
        if direction != 'legacy-sdk':
            await wait(lambda: (tmp_path / 'sdk-fresh.txt').exists())
            (directory / 'allow-legacy').touch()
        holder, waiter = ('legacy', 'sdk') if direction == 'legacy-sdk' else ('sdk', 'legacy')
        holder_role = legacy_role if holder == 'legacy' else sdk_role
        waiter_role = sdk_role if waiter == 'sdk' else legacy_role
        def waiting_attempts():
            requested = [e for e in events(waiter_role) if e['event'] == 'fresh_requested' and e['writer'] == waiter]
            return [e for e in events(waiter_role) if e['event'] == 'lock_attempt' and requested and e['time'] > requested[-1]['time']]
        await wait(waiting_attempts)
        attempt = waiting_attempts()[-1]
        # Disconnecting the observer must not cancel either production writer.
        for socket in sockets:
            await socket.close()
        sockets.clear()
        await asyncio.sleep(.5)
        for role in roles:
            sockets.append(await connect((directory / f'{role}.url').read_text()))
        legacy_ws, sdk_ws = sockets[0], sockets[-1]
        assert not (tmp_path / f'{waiter}-fresh.txt').exists(), directory
        assert not any(e['event'] == 'lock_acquired' and e['time'] > attempt['time'] for e in events(waiter_role)), directory
        assert (tmp_path / f'{holder}-fresh.txt').read_text() == f'{holder}-fresh'
        runtime = {role: [e for e in events(role) if e['event'] == 'runtime'][-1] for role in roles}
        for snapshot in runtime.values():
            assert snapshot['max_concurrent_sessions'] == 2
            assert len(snapshot['active_thread_ids']) <= 2
            assert not any(t.startswith('loop_') for t in snapshot['active_thread_ids'])
        assert root not in runtime[legacy_role]['active_thread_ids']

        identity = {}
        if goal:
            from tests.test_sdk.test_goal_mixed_writer_process import assert_goal_identity
            (directory / 'dump-history').touch()
            await wait(lambda: (directory / 'histories.json').exists())
            identity = assert_goal_identity(tmp_path, directory, events('legacy'), events('sdk'))
        if goal:
            (directory / 'blocked.json').write_text(json.dumps(dict(
                direction=direction, identity=identity, attempt=attempt, holder=holder, waiter=waiter,
                workspace=str(tmp_path), pids=[p.pid for p in processes],
                blocked_marker_absent=True, actual_tool_holder=True), indent=2))
        if holder == 'legacy':
            (directory / 'stop-legacy').touch()
            await wait(lambda: any(e['event'] == ('goal_stopped' if goal else 'loop_stopped') and e['stopped'] for e in events(legacy_role)))
            if goal:
                resources = next(e for e in events(legacy_role) if e['event'] == 'goal_stop_resources')
                assert resources['pump_done'], resources
                assert resources['tools'] and all(t['done'] for t in resources['tools']), resources
                assert resources['tool_cancel_acks'] == 1, resources
                assert resources['held_owners'] == 0, resources
        else:
            await rpc(sdk_ws, 'session.cancel', {'thread_id': sdk})
        await wait(lambda: (tmp_path / f'{waiter}-fresh.txt').exists())
        assert (tmp_path / f'{waiter}-fresh.txt').read_text() == f'{waiter}-fresh'
        if waiter == 'legacy' and not goal:
            await wait(lambda: any(e['event'] == 'fresh_returned' and e['writer'] == waiter for e in events(waiter_role)))
        acquired = next(e for e in events(waiter_role) if e['event'] == 'lock_acquired' and e['time'] > attempt['time'])
        released = [e for e in events(holder_role) if e['event'] == 'lock_closed' and e['held'] and attempt['time'] < e['time'] < acquired['time']]
        assert released, directory
        assert acquired['path'] == released[-1]['path'] == attempt['path']
        legacy_locks = [e for e in events(legacy_role) if e['event'] == 'lock_acquired' and e['thread_id'].startswith('goal:' if goal else 'loop:')]
        assert legacy_locks, directory
        import sqlite3
        with sqlite3.connect(tmp_path / '.voidx' / 'store' / 'voidx.db') as db:
            rows = db.execute('SELECT id, session_id FROM agent_threads').fetchall()
        from voidx.platform.session_ids import validate_session_storage_id
        children = [dict(thread_id=t, session_id=s) for t, s in rows if t.startswith('goal:' if goal else 'loop:')]
        assert children
        for child in children:
            assert validate_session_storage_id(child['session_id']) == child['session_id']
        (directory / 'result.json').write_text(json.dumps(dict(direction=direction, goal=goal, identity=identity,
            pids=[p.pid for p in processes], workspace=str(tmp_path), children=children,
            holder=holder, waiter=waiter, attempt=attempt, release=released[-1], acquired=acquired,
            runtime=runtime, disconnect_did_not_cancel=True, blocked_marker_absent=True, released_marker=f'{waiter}-fresh', evidence=str(directory)), indent=2))
        return directory
    finally:
        for writer in ('legacy', 'sdk'):
            (directory / f'release-{writer}').touch()
        for socket in sockets:
            await socket.close()
        for role in roles:
            (directory / f'shutdown-{role}').touch()
        for process in processes:
            try:
                await asyncio.wait_for(process.wait(), 15)
            except TimeoutError:
                process.kill()
                await process.wait()
        for stream in streams:
            stream.close()
