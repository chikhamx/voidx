"""Production autonomous composition, using the real LangGraph and runtime."""
import pytest
from langchain_core.messages import AIMessage

from tests.test_sdk.test_headless_execution import make_execution
from tests.test_sdk.test_headless_runtime import FileRoundTripModel
from voidx.agent.adapters.persistence.session_repository import create_session
from voidx.agent.domain.thread import AgentThread


@pytest.mark.asyncio
@pytest.mark.parametrize('profile', ['loop', 'goal'])
async def test_factory_runs_real_idle_without_starting_unapproved_generation(tmp_path, profile):
    from voidx.bootstrap.autonomous_headless import build_autonomous_session
    from voidx.agent.application.runtime.runtime import AgentRuntime
    from voidx.agent.application.automation.loop.scheduler import LoopRuntimeScheduler
    from voidx.agent.application.automation.goal.scheduler import GoalRuntimeScheduler

    class ReplyModel(FileRoundTripModel):
        def _reply(self, messages):
            return AIMessage(content='IDLE_ONLY')

    session = await create_session(workspace=str(tmp_path), profile=profile)
    execution, publisher = make_execution(tmp_path, session=session)
    execution.model = ReplyModel()
    assembly = build_autonomous_session(execution, workspace=str(tmp_path), session_id=session.id)
    assert isinstance(assembly.runtime, AgentRuntime)
    assert isinstance(assembly.loop_service._scheduler, LoopRuntimeScheduler)
    assert isinstance(assembly.goal_service._scheduler, GoalRuntimeScheduler)
    thread = AgentThread(thread_id=session.id, session_id=session.id, workspace=str(tmp_path))
    try:
        result = await assembly.run_idle(profile, 'hello', thread)
        assert result is None
        assert await assembly.loop_service.status(thread.thread_id) is None
        assert await assembly.goal_service.status(thread.thread_id) is None
        assert any(event.kind == 'assistant.committed' and event.payload.text == 'IDLE_ONLY'
                   for event in publisher.events)
        assert publisher.events[-1].kind == 'turn.completed'
    finally:
        await execution.aclose()


@pytest.mark.asyncio
async def test_sdk_coding_uses_production_runtime(tmp_path, monkeypatch):
    from voidx.sdk import VoidxAgent
    from voidx.config import Config, Settings
    from voidx.agent.application.runtime.runtime import AgentRuntime
    from voidx.llm.adapters import langchain_model_factory

    class ReplyModel(FileRoundTripModel):
        def _reply(self, messages):
            return AIMessage(content='RUNTIME_REPLY')

    calls = []
    original = AgentRuntime.run_turn

    async def record(self, request):
        calls.append(request)
        return await original(self, request)

    monkeypatch.setattr(AgentRuntime, 'run_turn', record)
    monkeypatch.setattr(langchain_model_factory, 'create_chat_model', lambda *a, **kw: ReplyModel())
    monkeypatch.setattr(langchain_model_factory, 'create_resolver_model', lambda *a, **kw: ReplyModel())
    config = Config(workspace=str(tmp_path))
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, 'local-test-not-a-credential')
    async with VoidxAgent(config, settings=settings) as agent:
        events = [event async for event in agent.stream('hello', workspace=str(tmp_path))]
    assert len(calls) == 1
    assert calls[0].thread.session_id == events[0].session_id
    assert events[-1].kind == 'turn.completed'


async def _managed_scheduler(tmp_path, profile, owner, runtime):
    from voidx.agent.adapters.persistence.thread_repository import ThreadStore
    from voidx.agent.application.automation.goal.scheduler import GoalRuntimeScheduler
    from voidx.agent.application.automation.loop.scheduler import LoopRuntimeScheduler
    from voidx.agent.domain.automation.goal import GOAL_PROFILE, GoalSpec, GoalState
    from voidx.agent.domain.automation.loop import LoopSpec
    from voidx.agent.domain.thread import AgentThreadState

    store = ThreadStore(tmp_path / 'scheduler.db')
    if profile == 'goal':
        spec = GoalSpec(objective='ship', acceptance_condition='tests pass', generation='owned')
        thread_id = spec.goal_thread_id('parent')
        state = GoalState.from_spec(spec, run_id='owned')
        await store.create_thread(
            AgentThread(thread_id=thread_id, workspace=str(tmp_path)),
            profile=GOAL_PROFILE,
            state=AgentThreadState(thread_id=thread_id, context={
                'goal_spec': spec.model_dump(mode='json'),
                'goal_run': state.model_dump(mode='json'),
            }),
        )
        scheduler = GoalRuntimeScheduler(store=store, runtime=runtime, workspace=str(tmp_path),
                                         owner=owner, pump_poll_seconds=.001)
        async def dispatch():
            return await scheduler.run_goal('parent', spec)
        payload = {'spec': spec.model_dump(mode='json'), 'goal_state': state.model_dump(mode='json')}
    else:
        spec = LoopSpec(prompt='ship')
        thread_id = spec.loop_thread_id('parent')
        scheduler = LoopRuntimeScheduler(store=store, runtime=runtime, workspace=str(tmp_path),
                                         owner=owner, pump_poll_seconds=.001)
        async def dispatch():
            return await scheduler.run_prompt('ship', display_text=None, session_id='parent', spec=spec)
        await scheduler._ensure_thread('parent', spec)
        payload = {'prompt': 'ship', 'spec': spec.model_dump(mode='json')}
    scheduler.register_managed_thread(thread_id)
    return scheduler, store, thread_id, dispatch, payload


@pytest.mark.asyncio
@pytest.mark.parametrize('profile', ['goal', 'loop'])
@pytest.mark.parametrize('budget', ['execution', 'renewal'])
async def test_scheduler_initial_dispatch_rejects_before_claim_or_side_effect(tmp_path, monkeypatch, profile, budget):
    import asyncio
    from voidx.agent.application.runtime.run_ownership import RunOwner

    owner = RunOwner(turns=1, producers=4)
    calls = []
    class Runtime:
        async def run_turn(self, *args, **kwargs):
            calls.append('runtime')
            raise AssertionError('capacity must reject before runtime')

    scheduler, store, thread_id, dispatch, _ = await _managed_scheduler(tmp_path, profile, owner, Runtime())
    for name in ('claim_outbox', 'claim_next_outbox', 'begin_attempt', 'mark_side_effect_started'):
        original = getattr(store, name)
        async def record(*args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            return await _original(*args, **kwargs)
        monkeypatch.setattr(store, name, record)
    held = owner.spawn(asyncio.Event().wait, role=budget)
    try:
        with pytest.raises(RuntimeError, match='capacity'):
            await dispatch()
        assert calls == []
        assert len(await store.list_pending_outbox(thread_id)) == 1
        assert not held.done()
    finally:
        await owner.aclose()
    assert owner.producer_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('profile', ['goal', 'loop'])
@pytest.mark.parametrize('source', ['initial', 'pump'])
async def test_scheduler_owned_dispatch_finish_drains_runner(tmp_path, profile, source):
    import asyncio
    from voidx.agent.application.runtime.run_ownership import RunOwner
    from voidx.agent.domain.thread import RuntimeDecision

    owner = RunOwner(turns=1, producers=4)
    entered, release = asyncio.Event(), asyncio.Event()
    completed, cancelled, registered = [], [], []
    class Runtime:
        async def run_turn(self, *args, **kwargs):
            registered.append(asyncio.current_task() in owner._dispatches)
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
            decision = RuntimeDecision(outcome='needs_user' if profile == 'loop' else 'completed', summary='done')
            if profile == 'loop':
                await args[0].context.loop_controller.submit_decision(decision)
            completed.append(True)
            return decision

    scheduler, store, thread_id, dispatch, payload = await _managed_scheduler(tmp_path, profile, owner, Runtime())
    finishing = task = None
    try:
        if source == 'pump':
            loaded = await store.load(thread_id)
            await store.enqueue_outbox(thread_id=thread_id, kind='wakeup', payload=payload,
                                       expected_state_version=loaded.state_version)
            scheduler.start_pump()
            task = scheduler._pump_task
            assert task in owner._tasks
        else:
            task = asyncio.create_task(dispatch())
        await asyncio.wait_for(entered.wait(), 2)
        assert registered == [True]
        assert owner._counts['execution'] == owner._counts['renewal'] == 1
        assert any(t.get_name() == 'run-renewal' for t in owner._tasks)
        finishing = asyncio.create_task(owner.finish())
        done, _ = await asyncio.wait({finishing}, timeout=.05)
        assert not done
        assert completed == cancelled == []
        release.set()
        await asyncio.wait_for(finishing, 2)
        await task
        assert completed == [True]
        assert cancelled == []
        assert await store.list_pending_outbox(thread_id) == []
        assert (await owner.completion).outcome == 'completed'
        await owner.aclose()
        assert owner.producer_count == 0
        assert not owner._tasks and not owner._dispatches
        assert all(count == 0 for count in owner._counts.values())
        assert task.done() and owner._lifecycle.done()
    finally:
        release.set()
        await owner.aclose()
        await scheduler.stop_pump()
        await asyncio.gather(*(t for t in (task, finishing) if t is not None), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_error', [False, True])
async def test_sdk_existing_loop_approval_two_iterations_complete(tmp_path, monkeypatch, cancel_error):
    """Only the provider is deterministic; SDK, graph, tools and storage are real."""
    import asyncio
    from pydantic import PrivateAttr
    from voidx.sdk import VoidxAgent
    from voidx.config import Config, PermissionMode, Settings
    from voidx.llm.adapters import langchain_model_factory
    from voidx.agent.application.runtime.runtime import AgentRuntime
    from voidx.agent.adapters.persistence.thread_repository import ThreadStore
    from voidx.tooling.domain.interaction import InteractionResponse

    class LoopModel(FileRoundTripModel):
        _step: int = PrivateAttr(default=0)

        def _reply(self, messages):
            self._histories.append(list(messages))
            script = [
                ('loop_init', {'goal': 'Complete exactly two iterations'}),
                (None, 'Approved loop ready.'),
                ('loop_start', {'goal': 'First iteration'}),
                ('loop_commit', {'outcome': 'continue', 'summary': 'First iteration done',
                                 'progress': 'meaningful', 'next_delay_seconds': 1}),
                ('loop_start', {'goal': 'Second iteration'}),
                ('loop_commit', {'outcome': 'continue', 'summary': 'Second iteration done',
                                 'progress': 'meaningful', 'next_delay_seconds': 60}),
            ]
            assert self._step < len(script), 'Unexpected extra model turn before loop completion'
            name, value = script[self._step]
            self._step += 1
            if name is None:
                return AIMessage(content=value)
            return AIMessage(content='', tool_calls=[{
                'id': f'loop-business-{self._step}', 'name': name, 'args': value,
            }])

    model = LoopModel()
    monkeypatch.setattr(langchain_model_factory, 'create_chat_model', lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, 'create_resolver_model', lambda *a, **kw: model)
    from voidx.agent.adapters.persistence.headless_locks import HeadlessFileLock
    from voidx.persistence.file_lock import acquire_file_lock_sync, release_file_lock_sync

    requests, executions = [], []
    locked_sessions = []
    original = AgentRuntime.run_turn

    async def record(self, request):
        requests.append(request)
        executions.append(self._resources.turn_engine._execution)
        probe = HeadlessFileLock('session', request.thread.session_id)
        try:
            handle = acquire_file_lock_sync(probe._path, blocking=False)
        except BlockingIOError:
            locked_sessions.append(request.thread.session_id)
        else:
            release_file_lock_sync(handle)
        return await original(self, request)

    monkeypatch.setattr(AgentRuntime, 'run_turn', record)
    session = await create_session(workspace=str(tmp_path), profile='loop')
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.PROJECT_TRUSTED)
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, 'local-test-not-a-credential')
    events, approvals = [], []
    owned_run = None
    async with VoidxAgent(config, settings=settings) as agent:
        async with asyncio.timeout(20):
            async for event in agent.stream('Complete exactly two iterations',
                                            session_id=session.id, workspace=str(tmp_path)):
                events.append(event)
                owned_run = agent._run
                if event.kind == 'turn.completed' and event.session_id != session.id:
                    child_terminals = [item for item in events if item.kind == 'turn.completed'
                                       and item.session_id != session.id]
                    probe = HeadlessFileLock('session', event.session_id)
                    await asyncio.wait_for(probe.acquire(), 1)
                    probe.release()
                    if len(child_terminals) == 2:
                        if cancel_error:
                            original_cancel = owned_run.owner.cancel
                            async def broken_cancel():
                                await original_cancel()
                                raise ValueError('owner cancel failed')
                            monkeypatch.setattr(owned_run.owner, 'cancel', broken_cancel)
                            with pytest.raises(ValueError, match='owner cancel failed'):
                                await agent.cancel(session_id=session.id)
                            # The close task retains the error; avoid re-awaiting it on stream exit.
                            monkeypatch.setattr(owned_run, 'cancel', original_cancel)
                        else:
                            await agent.cancel(session_id=session.id)
                if event.kind == 'interaction.required':
                    request = event.payload.request
                    assert request.purpose == 'loop'
                    assert request.session_id == session.id
                    assert request.thread_id == session.id
                    assert request.turn_id == events[0].turn_id
                    approvals.append(request)
                    assert await agent.submit_interaction(request.interaction_id, InteractionResponse(
                        session_id=request.session_id, thread_id=request.thread_id,
                        turn_id=request.turn_id, value='approved',
                    ))
    assert len(approvals) == 1, (
        'SDK loop intake never requested approval; event kinds: '
        + repr([(event.kind, event.payload) for event in events])
    )
    assert len(requests) == 3, 'stream ended before idle plus two real loop iterations'
    assert len({id(execution) for execution in executions}) == 3
    assert len({id(execution.graph) for execution in executions}) == 3
    children = requests[1:]
    assert all(request.thread.session_id != session.id for request in children)
    assert children[0].thread.thread_id == children[1].thread.thread_id
    started = [event for event in events if event.kind == 'turn.started']
    assert len(started) == 3
    assert len({event.turn_id for event in started}) == 3
    assert {event.session_id for event in started[1:]} == {children[0].thread.session_id}
    completed = [event for event in events if event.kind == 'turn.completed']
    assert {event.turn_id for event in completed} == {event.turn_id for event in started}
    assert not any(event.kind in ('turn.failed', 'turn.cancelled') for event in events)
    loaded = await ThreadStore().load(children[-1].thread.thread_id)
    assert loaded is not None
    assert loaded.state.lifecycle.value == 'cancelled'
    assert loaded.state.lifecycle_decision.outcome == 'stop'
    assert await ThreadStore().list_pending_outbox(loaded.thread.thread_id) == []
    assert owned_run._interactions == {}
    assert owned_run._workspace_lock._closed
    assert owned_run._workspace_lock._lock._handle is None
    assert owned_run._session_lock._handle is None
    assert owned_run._driver.done()
    assert owned_run.owner.producer_count == 0
    assert owned_run.owner.mux.registered == 0
    assert (await owned_run.owner.completion).outcome == 'cancelled'

    assert locked_sessions == [request.thread.session_id for request in requests]


@pytest.mark.asyncio
@pytest.mark.parametrize('race_cancel', [False, True])
async def test_sdk_goal_approval_two_attempts_natural_completion(tmp_path, monkeypatch, race_cancel):
    import asyncio
    from pydantic import PrivateAttr
    from voidx.sdk import VoidxAgent
    from voidx.config import Config, PermissionMode, Settings
    from voidx.llm.adapters import langchain_model_factory
    from voidx.agent.application.runtime.runtime import AgentRuntime
    from voidx.agent.adapters.persistence.thread_repository import ThreadStore
    from voidx.agent.adapters.persistence.headless_locks import HeadlessFileLock
    from voidx.persistence.file_lock import acquire_file_lock_sync, release_file_lock_sync
    from voidx.tooling.domain.interaction import InteractionResponse

    from voidx.agent.application.runtime.run_ownership import RunOwner
    original_finish = RunOwner.finish
    cancel_tasks = []

    async def delayed_finish(self):
        await original_finish(self)
        if race_cancel:
            cancel_tasks.extend(asyncio.create_task(agent.cancel(session_id=session.id)) for _ in range(2))
        # Completion publication may precede asynchronous finish cleanup.
        await asyncio.sleep(.05)

    monkeypatch.setattr(RunOwner, 'finish', delayed_finish)

    class GoalModel(FileRoundTripModel):
        _step: int = PrivateAttr(default=0)

        def _reply(self, messages):
            script = [
                ('goal_init', {'goal': 'Verify two attempts', 'acceptance_condition': 'Second evidence verified', 'max_attempts': 3}),
                ('goal_checkpoint', {'summary': 'First work', 'progress': 'meaningful', 'evidence': ['first']}),
                ('goal_decision', {'status': 'continue', 'summary': 'Need second evidence', 'progress': 'meaningful', 'next_hint': 'Second work'}),
                (None, 'Continue with second work.'),
                ('goal_checkpoint', {'summary': 'Second work', 'progress': 'meaningful', 'evidence': ['second']}),
                ('goal_decision', {'status': 'finished', 'summary': 'Second evidence verified', 'evidence': ['second'], 'progress': 'meaningful'}),
                (None, 'Goal completed.'),
            ]
            name, value = script[min(self._step, len(script) - 1)]
            self._step += 1
            # An unassembled goal lacks its controller; end that turn for a business RED.
            if self._step > len(script):
                return AIMessage(content='No autonomous controller.')
            return AIMessage(content=value) if name is None else AIMessage(content='', tool_calls=[
                {'id': f'goal-business-{self._step}', 'name': name, 'args': value}])

    model = GoalModel()
    monkeypatch.setattr(langchain_model_factory, 'create_chat_model', lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, 'create_resolver_model', lambda *a, **kw: model)
    requests, executions, locked = [], [], []
    original = AgentRuntime.run_turn

    async def record(self, request):
        requests.append(request)
        executions.append(self._resources.turn_engine._execution)
        probe = HeadlessFileLock('session', request.thread.session_id)
        try:
            handle = acquire_file_lock_sync(probe._path, blocking=False)
        except BlockingIOError:
            locked.append(request.thread.session_id)
        else:
            release_file_lock_sync(handle)
        return await original(self, request)

    monkeypatch.setattr(AgentRuntime, 'run_turn', record)
    session = await create_session(workspace=str(tmp_path), profile='goal')
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.PROJECT_TRUSTED)
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, 'local-test-not-a-credential')
    events, approvals = [], []
    async with VoidxAgent(config, settings=settings) as agent:
        async with asyncio.timeout(30):
            async for event in agent.stream('Verify two attempts', session_id=session.id, workspace=str(tmp_path)):
                events.append(event)
                run = agent._run
                if event.kind == 'interaction.required':
                    request = event.payload.request
                    assert request.purpose == 'goal'
                    with pytest.raises(ValueError, match='ownership'):
                        await agent.submit_interaction(request.interaction_id, InteractionResponse(
                            session_id=request.session_id, thread_id=request.thread_id,
                            turn_id='wrong-turn', value='approved'))
                    assert (request.session_id, request.thread_id, request.turn_id) == (session.id, session.id, events[0].turn_id)
                    approvals.append(request)
                    assert await agent.submit_interaction(request.interaction_id, InteractionResponse(
                        session_id=request.session_id, thread_id=request.thread_id,
                        turn_id=request.turn_id, value='approved'))
    assert len(approvals) == 1, 'SDK goal intake never requested approval'
    assert len(requests) == 5
    assert [request.context.goal_phase for request in requests] == ['idle', 'work', 'evaluator', 'work', 'evaluator']
    assert requests[1].thread.session_id == requests[3].thread.session_id
    assert requests[2].thread.session_id == requests[4].thread.session_id
    assert len({request.thread.session_id for request in requests}) == 3
    assert len({id(item) for item in executions}) == 5
    assert len({id(item.graph) for item in executions}) == 5
    assert locked == [request.thread.session_id for request in requests]
    started = [event for event in events if event.kind == 'turn.started']
    terminals = [event for event in events if event.kind in ('turn.completed', 'turn.failed', 'turn.cancelled')]
    assert len(started) == len(terminals) == 5
    assert len({event.turn_id for event in started}) == 5
    assert {event.turn_id for event in terminals} == {event.turn_id for event in started}
    assert all(event.kind == 'turn.completed' for event in terminals)
    loaded = await ThreadStore().load(requests[1].thread.thread_id)
    assert loaded.state.lifecycle.value == 'completed'
    assert loaded.state.context['goal_run']['attempt_count'] == 2
    assert await ThreadStore().list_pending_outbox(loaded.thread.thread_id) == []
    assert (await run.owner.completion).outcome == 'completed'
    assert run._driver.done()
    assert run.owner.producer_count == run.owner.mux.registered == 0
    assert run._interactions == {}
    assert run._session_lock._handle is None
    assert run._workspace_lock._closed
    assert run._workspace_lock._lock._handle is None

    assert run._completion_task is not None and run._completion_task.done()
    assert await asyncio.gather(*cancel_tasks) == [None] * len(cancel_tasks)
