"""Goal completion waits on durable scheduler notification, not status polling."""
import asyncio
import threading

import pytest

from tests.test_sdk.test_autonomous_session_recovery import RecoveryModel, sdk, seed_goal


@pytest.mark.asyncio
async def test_sdk_waits_without_polling_while_real_work_is_paused(tmp_path, monkeypatch):
    from voidx.agent.application.automation.goal.goal_service import GoalService

    session, store, binding = await seed_goal(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    reads = []
    original = GoalService.status

    async def status(self, *args, **kwargs):
        reads.append(args)
        return await original(self, *args, **kwargs)

    class PausedModel(RecoveryModel):
        def _reply(self, messages):
            if self._step == 0:
                entered.set()
                if not release.wait(10):
                    raise RuntimeError('test work release timed out')
            return super()._reply(messages)

    monkeypatch.setattr(GoalService, 'status', status)
    agent = sdk(tmp_path, monkeypatch, PausedModel())
    events = []

    async def consume():
        async for event in agent.stream('Recover', session_id=session.id, workspace=str(tmp_path)):
            events.append(event)

    task = asyncio.create_task(consume())
    try:
        async with asyncio.timeout(20):
            assert await asyncio.to_thread(entered.wait, 8)
            baseline = len(reads)
            await asyncio.sleep(.15)
            assert not task.done(), 'intake/recovery is not goal completion'
            assert len(reads) == baseline, 'SDK periodically reads goal.status during work'
    finally:
        release.set()
        await asyncio.wait_for(task, 20)
        await agent.aclose()
    assert (await store.load(binding.goal_thread_id)).state.lifecycle.value == 'completed'
    assert events[-1].kind == 'turn.completed'


@pytest.mark.asyncio
@pytest.mark.parametrize('lifecycle', ['completed', 'blocked', 'failed', 'cancelled'])
async def test_committed_terminal_is_sticky_and_follows_status_output(lifecycle):
    from types import SimpleNamespace
    from voidx.agent.application.runtime.scheduler_events import OwnedSchedulerEvents

    entered, release = asyncio.Event(), asyncio.Event()
    # A real semantic channel supplies the event identity and validation.
    from voidx.agent.application.runtime.semantic_channel import SemanticChannel
    channel = SemanticChannel(turn_id='turn', session_id='s', thread_id='t')
    original = channel.publish
    async def publish(event):
        entered.set()
        await release.wait()
        await original(event)
    channel.publish = publish
    owner = SimpleNamespace(dispatch_channel=lambda: channel)
    events = OwnedSchedulerEvents(owner)
    from voidx.agent.application.runtime.contracts import GoalPhaseResult
    phase = GoalPhaseResult(phase='work', attempt_number=1, reason='')
    waiting = asyncio.create_task(events.wait_goal_terminal())
    committing = asyncio.create_task(events.committed(goal_phase=phase, lifecycle=lifecycle))
    await asyncio.wait_for(entered.wait(), 2)
    assert not waiting.done()
    release.set()
    await committing
    assert await asyncio.wait_for(waiting, 2) == lifecycle
    assert await asyncio.wait_for(events.wait_goal_terminal(), 2) == lifecycle


@pytest.mark.asyncio
async def test_sdk_recovers_projected_terminal_without_new_intake(tmp_path, monkeypatch):
    from tests.goal_protocol_helpers import submit_fenced_goal_protocol
    from voidx.agent.application.automation.goal.projector import GoalProjector
    from voidx.agent.domain.automation.goal import GoalDecision, GoalProtocolRecord, WorkCheckpoint
    from voidx.agent.domain.thread import TERMINAL_LIFECYCLES

    session, store, binding = await seed_goal(tmp_path, monkeypatch)
    checkpoint = GoalProtocolRecord.submitted(
        protocol_id='recovery-checkpoint', parent_session_id=session.id,
        generation=binding.generation, phase='checkpoint', attempt_number=1,
        turn_id='crashed-work', session_id=binding.work_session_id,
        payload=WorkCheckpoint(generation=binding.generation, attempt_number=1,
                               summary='Work captured', work_turn_id='crashed-work'),
    )
    await submit_fenced_goal_protocol(store, checkpoint)
    await GoalProjector(store=store).project(checkpoint.protocol_id)
    decision = GoalProtocolRecord.submitted(
        protocol_id='recovery-terminal-decision', parent_session_id=session.id,
        generation=binding.generation, phase='decision', attempt_number=1,
        turn_id='crashed-evaluator', session_id=binding.evaluator_session_id,
        payload=GoalDecision(generation=binding.generation, attempt_number=1,
                             status='finished', summary='Verified'),
    )
    _, attempt = await submit_fenced_goal_protocol(store, decision)
    assert (await store.load(binding.goal_thread_id)).state.lifecycle not in TERMINAL_LIFECYCLES
    assert (await store.get_goal_protocol(decision.protocol_id)).status == 'submitted'
    model = RecoveryModel()
    async with sdk(tmp_path, monkeypatch, model) as agent:
        async with asyncio.timeout(10):
            events = [event async for event in agent.stream(
                'Recover terminal', session_id=session.id, workspace=str(tmp_path))]
    assert model._step == 0
    assert events == []
    assert (await store.load(binding.goal_thread_id)).state.lifecycle.value == 'completed'
    assert (await store.get_goal_protocol(decision.protocol_id)).status == 'projected'
    assert (await store.get_attempt(attempt.attempt_id)).status == 'committed'
    assert len(await store.list_goal_generations(session.id)) == 1


@pytest.mark.asyncio
async def test_sdk_durable_commit_failure_never_notifies_success(tmp_path, monkeypatch):
    from voidx.agent.adapters.persistence.thread_repository import ThreadStore
    from voidx.agent.application.runtime.scheduler_events import OwnedSchedulerEvents

    session, store, binding = await seed_goal(tmp_path, monkeypatch)
    notifications = []
    original = OwnedSchedulerEvents.committed

    async def observe(self, **kwargs):
        notifications.append(kwargs.get('lifecycle'))
        await original(self, **kwargs)

    async def fail_commit(self, **kwargs):
        raise OSError('injected durable phase commit failure')

    monkeypatch.setattr(OwnedSchedulerEvents, 'committed', observe)
    monkeypatch.setattr(ThreadStore, 'commit_goal_phase', fail_commit)
    async with sdk(tmp_path, monkeypatch, RecoveryModel()) as agent:
        with pytest.raises(RuntimeError, match='Run failed'):
            async with asyncio.timeout(10):
                async for _ in agent.stream('Recover', session_id=session.id, workspace=str(tmp_path)):
                    run = agent._run
        assert run.owner.producer_count == 0
    assert 'completed' not in notifications
    assert (await store.load(binding.goal_thread_id)).state.lifecycle.value != 'completed'
