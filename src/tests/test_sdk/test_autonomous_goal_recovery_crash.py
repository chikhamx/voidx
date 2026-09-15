"""SDK evaluator process death after real tool submission, before projection."""
import asyncio
import json
import os
import subprocess
import sys

import pytest
from langchain_core.messages import AIMessage
from pydantic import PrivateAttr

from tests.test_sdk.test_autonomous_session_recovery import sdk, assert_root_released
from tests.test_sdk.test_headless_runtime import FileRoundTripModel
from voidx.agent.adapters.persistence.session_repository import create_session
from voidx.agent.adapters.persistence.thread_repository import ThreadStore
from voidx.tooling.domain.interaction import InteractionResponse


class GoalCrashModel(FileRoundTripModel):
    _step: int = PrivateAttr(default=0)

    def _reply(self, messages):
        self._histories.append(list(messages))
        script = [
            ('goal_init', {'goal': 'Verify outcome', 'acceptance_condition': 'Evidence verified', 'max_attempts': 1}),
            ('goal_checkpoint', {'summary': 'Work verified', 'progress': 'meaningful', 'evidence': ['work']}),
            ('goal_decision', {'status': 'finished', 'summary': 'Evidence checked', 'progress': 'meaningful', 'evidence': ['work']}),
        ]
        assert self._step < len(script), 'Submitted evaluator or intake was rerun'
        name, args = script[self._step]
        self._step += 1
        return AIMessage(content='', tool_calls=[{'id': f'crash-{self._step}', 'name': name, 'args': args}])


async def _crash_process(workspace, session_id, crash_phase="decision"):
    import voidx.persistence.sqlite as sqlite
    from voidx.config.settings import Settings
    from voidx.config.adapters.profile_store import MemoryModelProfileStore

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(sqlite, 'DATA_DIR', workspace / '.voidx')
    monkeypatch.setattr('voidx.config.settings._settings_home', lambda: workspace)
    original_settings = Settings.__init__
    monkeypatch.setattr(Settings, '__init__', lambda self, *a, **kw: original_settings(
        self, *a, **{**kw, 'profile_store': MemoryModelProfileStore()}))
    submit = ThreadStore.submit_goal_protocol
    model = GoalCrashModel()
    approvals = []

    async def crash(self, record, **kwargs):
        result = await submit(self, record, **kwargs)
        if record.phase == crash_phase:
            binding = await self.get_goal_generation(record.generation)
            assert result.status == 'submitted'
            (workspace / 'goal-crash.json').write_text(json.dumps({
                'binding': binding.model_dump(mode='json'), 'protocol_id': result.protocol_id,
                'steps': model._step, 'approvals': approvals,
            }))
            os._exit(73)
        return result

    monkeypatch.setattr(ThreadStore, 'submit_goal_protocol', crash)
    async with sdk(workspace, monkeypatch, model) as agent:
        async with asyncio.timeout(20):
            async for event in agent.stream('Verify outcome', session_id=session_id, workspace=str(workspace)):
                if event.kind == 'interaction.required':
                    request = event.payload.request
                    approvals.append(request.interaction_id)
                    assert await agent.submit_interaction(request.interaction_id, InteractionResponse(
                        session_id=request.session_id, thread_id=request.thread_id,
                        turn_id=request.turn_id, value='approved'))
    raise AssertionError('Evaluator submission boundary not reached')


@pytest.mark.asyncio
@pytest.mark.parametrize('lease_loss', [None, 'expired', 'new-owner', 'project-new-owner', 'summary-new-owner', 'summary-work-new-owner', 'handoff-new-owner'])
async def test_sdk_goal_evaluator_crash_recovery(tmp_path, monkeypatch, lease_loss):
    work_crash = lease_loss in ("summary-work-new-owner", "handoff-new-owner")
    session = await create_session(workspace=str(tmp_path), profile='goal')
    command = [sys.executable, '-c',
               'import asyncio,sys; from pathlib import Path; '
               'from tests.test_sdk.test_autonomous_goal_recovery_crash import _crash_process; '
               'asyncio.run(_crash_process(Path(sys.argv[1]),sys.argv[2],sys.argv[3]))', str(tmp_path), session.id, 'checkpoint' if work_crash else 'decision']
    process = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True, timeout=40)
    assert process.returncode == 73, process.stdout + process.stderr
    snapshot = json.loads((tmp_path / 'goal-crash.json').read_text())
    assert snapshot['steps'] == (2 if work_crash else 3)
    assert len(snapshot['approvals']) == 1
    store = ThreadStore()
    binding = (await store.list_goal_generations(session.id))[0]
    assert binding.model_dump(mode='json') == snapshot['binding']
    generation = binding.generation
    records = await store.list_goal_protocols(generation)
    assert [(r.phase, r.status) for r in records] == ([
        ('init', 'projected'), ('checkpoint', 'submitted')] if work_crash else [
        ('init', 'projected'), ('checkpoint', 'projected'), ('decision', 'submitted')])

    async def durable():
        return {
            'state': dict(await store._one('SELECT * FROM agent_thread_state WHERE thread_id = ?', (binding.goal_thread_id,))),
            'thread': dict(await store._one('SELECT * FROM agent_threads WHERE id = ?', (binding.goal_thread_id,))),
            'records': [r.model_dump(mode='json') for r in await original_list(store, generation)],
            'summaries': [dict(r) for r in await store._all('SELECT * FROM goal_public_summary_outbox ORDER BY summary_id', ())],
            'messages': (tmp_path / '.voidx' / 'sessions' / session.id / 'messages.jsonl').read_bytes(),
            'outbox': [dict(r) for r in await store._all('SELECT * FROM runtime_outbox ORDER BY id', ())],
        }

    original_list = ThreadStore.list_goal_protocols
    original_project = ThreadStore.project_goal_protocol
    baseline = await durable()
    injected = False
    foreign_lease = None
    projections = []

    async def lose_lease(self, current_generation):
        nonlocal injected, foreign_lease
        result = await original_list(self, current_generation)
        if lease_loss in ('expired', 'new-owner') and not injected and current_generation == generation:
            injected = True
            await self._write(lambda conn: conn.execute(
                'UPDATE goal_recovery_leases SET lease_expires_at = 0 WHERE generation = ?', (generation,)))
            if lease_loss == 'new-owner':
                assert await self.acquire_goal_generation_lease(generation, 'legitimate-new-owner', lease_seconds=60)
                foreign_lease = dict(await self._one('SELECT * FROM goal_recovery_leases WHERE generation = ?', (generation,)))
        return result

    async def project(self, protocol_id, **kwargs):
        nonlocal injected, foreign_lease
        if lease_loss == 'project-new-owner':
            lease = await self.get_goal_generation_lease(generation)
            assert await self.renew_goal_generation_lease(generation, lease['lease_owner'], lease_seconds=30)
            injected = True
            await self._write(lambda conn: conn.execute(
                'UPDATE goal_recovery_leases SET lease_expires_at = 0 WHERE generation = ?', (generation,)))
            assert await self.acquire_goal_generation_lease(generation, 'legitimate-new-owner', lease_seconds=60)
            foreign_lease = await self.get_goal_generation_lease(generation)
        result = await original_project(self, protocol_id, **kwargs)
        projections.append(protocol_id)
        return result

    from voidx.agent.application.automation.goal.recovery import GoalRecovery
    from voidx.agent.application.automation.goal.goal_service import GoalService
    original_recover = GoalRecovery.recover_generation
    side_effects = []

    async def recover(self, *args, **kwargs):
        nonlocal baseline, injected, foreign_lease
        result = await original_recover(self, *args, **kwargs)
        if lease_loss in ('summary-new-owner', 'summary-work-new-owner'):
            baseline = await durable()
            injected = True
            await store._write(lambda conn: conn.execute(
                'UPDATE goal_recovery_leases SET lease_expires_at = 0 WHERE generation = ?', (generation,)))
            assert await store.acquire_goal_generation_lease(generation, 'legitimate-new-owner', lease_seconds=60)
            foreign_lease = await store.get_goal_generation_lease(generation)
        return result

    original_deliver = ThreadStore.deliver_goal_public_summaries

    async def deliver(self, **kwargs):
        nonlocal baseline, injected, foreign_lease
        result = await original_deliver(self, **kwargs)
        if lease_loss == 'handoff-new-owner' and kwargs.get('generation') == generation:
            baseline = await durable()
            injected = True
            await store._write(lambda conn: conn.execute(
                'UPDATE goal_recovery_leases SET lease_expires_at = 0 WHERE generation = ?', (generation,)))
            assert await store.acquire_goal_generation_lease(generation, 'legitimate-new-owner', lease_seconds=60)
            foreign_lease = await store.get_goal_generation_lease(generation)
        return result

    monkeypatch.setattr(ThreadStore, 'deliver_goal_public_summaries', deliver)
    monkeypatch.setattr(GoalRecovery, 'recover_generation', recover)
    for name in ('_register_thread', '_start_pump', 'stop'):
        original = getattr(GoalService, name)
        if name == 'stop':
            async def stop(self, *args, _original=original, **kwargs):
                side_effects.append('stop')
                return await _original(self, *args, **kwargs)
            monkeypatch.setattr(GoalService, name, stop)
        else:
            def effect(self, *args, _name=name, _original=original, **kwargs):
                side_effects.append(_name)
                return _original(self, *args, **kwargs)
            monkeypatch.setattr(GoalService, name, effect)

    monkeypatch.setattr(ThreadStore, 'list_goal_protocols', lose_lease)
    monkeypatch.setattr(ThreadStore, 'project_goal_protocol', project)
    model = GoalCrashModel()
    events, errors, runs = [], [], []
    from voidx.agent.application.runtime.run_ownership import RunOwner
    original_init = RunOwner.__init__

    def capture(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        runs.append(self)

    monkeypatch.setattr(RunOwner, '__init__', capture)
    async with sdk(tmp_path, monkeypatch, model) as agent:
        try:
            async with asyncio.timeout(15):
                async for event in agent.stream('Do not restart intake', session_id=session.id, workspace=str(tmp_path)):
                    events.append(event)
        except Exception as exc:
            errors.append(exc)
    assert model._step == 0
    assert not any(e.kind == 'interaction.required' for e in events)
    assert len(runs) == 1
    assert runs[0].completion.done()
    assert runs[0].producer_count == 0
    await assert_root_released(session.id)
    if lease_loss:
        assert injected
        assert errors and str(errors[0]) == 'Run failed', errors
        assert await durable() == baseline
        assert side_effects == []
        assert projections == ([snapshot['protocol_id']] if lease_loss in ('summary-new-owner', 'summary-work-new-owner', 'handoff-new-owner') else [])
        assert (await runs[0].completion).outcome == 'failed'
        if foreign_lease:
            assert dict(await store._one('SELECT * FROM goal_recovery_leases WHERE generation = ?', (generation,))) == foreign_lease
    else:
        assert not errors, errors
        assert projections == [snapshot['protocol_id']]
        assert (await runs[0].completion).outcome == 'completed'
        after = await store.load(binding.goal_thread_id)
        assert after.state.lifecycle.value == 'completed'
        assert after.state.context['goal_run']['projected_sequence_number'] == 2
        assert len(await store.list_goal_generations(session.id)) == 1
        recovered_binding = (await store.get_goal_generation(generation)).model_dump(mode='json')
        assert recovered_binding.pop('terminal_at') is not None
        assert recovered_binding == {k: v for k, v in snapshot['binding'].items() if k != 'terminal_at'}
        assert [(r.phase, r.status) for r in await original_list(store, generation)] == [
            ('init', 'projected'), ('checkpoint', 'projected'), ('decision', 'projected')]
