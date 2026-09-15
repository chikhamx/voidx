"""Explicit owned close preserves bounded original cleanup failures."""
import asyncio
import traceback

import pytest
from langchain_core.messages import AIMessage
from pydantic import PrivateAttr

from tests.test_sdk.test_headless_runtime import FileRoundTripModel
from voidx.agent.application.runtime.run_ownership import RunOwner


def leaves(error):
    if isinstance(error, BaseExceptionGroup):
        return [leaf for child in error.exceptions for leaf in leaves(child)]
    return [error]


@pytest.mark.asyncio
@pytest.mark.parametrize('close', ['cancel', 'aclose'])
async def test_owner_close_collects_child_and_owner_errors(close):
    entered = asyncio.Event()
    errors = [OSError('child secret'), ValueError('owner secret')]
    released = []

    async def execute(_):
        entered.set()
        await asyncio.Event().wait()

    async def cleanup(_):
        released.append('child')
        raise errors[0]

    async def owner_cleanup():
        released.append('owner')
        raise errors[1]

    owner = RunOwner(cleanup=owner_cleanup)
    supervisor = await owner.register_turn(session_id='s', thread_id='t', execute=execute,
                                           persist=None, cleanup=cleanup)
    await entered.wait()
    with pytest.raises(BaseExceptionGroup) as caught:
        await getattr(owner, close)()
    assert leaves(caught.value) == errors
    assert released == ['child', 'owner']
    assert (await owner.completion).outcome == 'failed'
    from voidx.agent.domain.semantic_events import decode_event
    terminal = decode_event((await supervisor.channel.completion).terminal)
    assert terminal.payload.summary == 'Run failed'
    with pytest.raises(BaseExceptionGroup) as repeated:
        await owner.aclose()
    assert leaves(repeated.value) == errors
    assert released == ['child', 'owner']


@pytest.mark.asyncio
@pytest.mark.parametrize('multiple', [False, True])
@pytest.mark.parametrize('close', ['cancel', 'aclose', None])
async def test_real_sdk_child_close_errors_release_all_resources(tmp_path, monkeypatch, multiple, close):
    from voidx.sdk import VoidxAgent
    from voidx.config import Config, PermissionMode, Settings
    from voidx.llm.adapters import langchain_model_factory
    from voidx.agent.adapters.persistence.session_repository import create_session
    from voidx.agent.adapters.persistence.headless_locks import HeadlessFileLock
    from voidx.agent.application.runtime.runtime import AgentRuntime
    from voidx.tooling.domain.interaction import InteractionResponse
    from voidx.bootstrap.autonomous_headless import _SDKLoopRun

    class Model(FileRoundTripModel):
        _step: int = PrivateAttr(default=0)

        def _reply(self, messages):
            script = [('loop_init', {'goal': 'cleanup test'}), (None, 'ready'),
                      ('loop_start', {'goal': 'child'}), (None, 'child reply')]
            name, args = script[min(self._step, len(script) - 1)]
            self._step += 1
            return (AIMessage(content=args) if name is None else
                    AIMessage(content='', tool_calls=[{'name': name, 'args': args,
                                                      'id': f'call-{self._step}'}]))

    model = Model()
    monkeypatch.setattr(langchain_model_factory, 'create_chat_model', lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, 'create_resolver_model', lambda *a, **kw: model)
    session = await create_session(workspace=str(tmp_path), profile='loop')
    errors = [OSError('execution secret')]
    if multiple:
        errors = [ValueError('interaction secret'), *errors, RuntimeError('session secret')]
    captured, released, events = [], [], []
    original = AgentRuntime.run_turn
    original_release = HeadlessFileLock.release
    original_close = _SDKLoopRun._close

    async def close_run(self):
        captured.append(self)
        stop = self.assembly.loop_service.stop

        async def record_stop(session_id):
            await stop(session_id)
            released.append('stop')

        monkeypatch.setattr(self.assembly.loop_service, 'stop', record_stop)
        await original_close(self)
        released.append('owner')

    def release(self):
        original_release(self)
        if self._path == child_path[0]:
            released.append('session')
            if multiple:
                raise errors[-1]

    child_path = [None]

    async def record(self, request):
        if request.thread.session_id != session.id:
            execution = self._resources.turn_engine._execution
            child_path[0] = HeadlessFileLock('session', request.thread.session_id)._path
            original_aclose = execution.aclose

            async def broken_close():
                await original_aclose()
                released.append('execution')
                raise errors[1 if multiple else 0]

            monkeypatch.setattr(execution, 'aclose', broken_close)
            if multiple:
                from voidx.agent.application.runtime.interaction_coordinator import InteractionCoordinator
                cleanup = InteractionCoordinator.cleanup

                async def broken_cleanup(coordinator, outcome):
                    await cleanup(coordinator, outcome)
                    released.append('interaction')
                    raise errors[0]

                monkeypatch.setattr(InteractionCoordinator, 'cleanup', broken_cleanup)
        return await original(self, request)

    monkeypatch.setattr(AgentRuntime, 'run_turn', record)
    monkeypatch.setattr(HeadlessFileLock, 'release', release)
    monkeypatch.setattr(_SDKLoopRun, '_close', close_run)
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.PROJECT_TRUSTED)
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, 'local-test-not-a-credential')
    agent = VoidxAgent(config, settings=settings)
    with pytest.raises(RuntimeError, match="^Run failed$") as caught:
        async with asyncio.timeout(20):
            async for event in agent.stream('cleanup test', session_id=session.id, workspace=str(tmp_path)):
                events.append(event)
                if event.kind == 'turn.failed' and close is not None:
                    with pytest.raises(BaseExceptionGroup) as explicit:
                        await getattr(agent, close)()
                    assert leaves(explicit.value) == errors
                if event.kind == 'interaction.required':
                    request = event.payload.request
                    assert await agent.submit_interaction(request.interaction_id, InteractionResponse(
                        session_id=request.session_id, thread_id=request.thread_id,
                        turn_id=request.turn_id, value='approved'))
    assert type(caught.value) is RuntimeError
    assert "secret" not in "".join(traceback.format_exception(caught.value))
    run = captured[0]
    assert (await run.owner.completion).outcome == 'failed'
    assert run.owner.producer_count == 0
    assert run._interactions == {}
    assert run._session_lock._handle is None
    assert run._workspace_lock._closed
    assert run._workspace_lock._lock._handle is None
    with pytest.raises(BaseExceptionGroup) as repeated:
        await run.cancel()
    assert leaves(repeated.value) == errors
    assert released[-1] == 'owner'
    assert 'execution' in released and 'session' in released and 'stop' in released
    probe = HeadlessFileLock('session', next(e.session_id for e in events if e.session_id != session.id))
    await asyncio.wait_for(probe.acquire(), 1)
    original_release(probe)
    failures = [e for e in events if e.kind == 'turn.failed']
    assert failures and all(e.payload.summary == 'Run failed' for e in failures)
    assert len({e.turn_id for e in failures}) == len(failures)


@pytest.mark.asyncio
async def test_owner_cleanup_error_storage_is_bounded():
    owner = RunOwner(turns=1, producers=4, interactions=1)
    originals = [OSError(str(i)) for i in range(100)]
    # A single resource may already aggregate more failures than the run budget.
    async def execute(_):
        await asyncio.Event().wait()

    async def cleanup(_):
        raise ExceptionGroup('resource', originals)

    await owner.register_turn(session_id='s', thread_id='t', execute=execute,
                              persist=None, cleanup=cleanup)
    with pytest.raises(BaseExceptionGroup) as caught:
        await owner.cancel()
    assert leaves(caught.value) == originals[:owner._cleanup_error_limit]
    assert len(leaves(caught.value)) <= 4 + 1 + 1 + 4
    assert owner._cleanup_errors_dropped == 100 - len(leaves(caught.value))
    assert owner._cleanup_errors_dropped_by_source == {
        "turn": 100 - len(leaves(caught.value)), "owner": 0,
    }
    assert (await owner.completion).outcome == 'failed'


@pytest.mark.asyncio
async def test_owner_keeps_cleaning_other_supervisors_before_completion():
    entered = [asyncio.Event(), asyncio.Event()]
    closing, release = asyncio.Event(), asyncio.Event()
    errors = [OSError('one'), ValueError('two')]
    cleaned = []

    async def owner_cleanup():
        closing.set()
        await release.wait()
        cleaned.append('owner')

    owner = RunOwner(cleanup=owner_cleanup)
    for index in range(2):
        async def execute(_, i=index):
            entered[i].set()
            await asyncio.Event().wait()

        async def cleanup(_, i=index):
            cleaned.append(i)
            raise errors[i]

        await owner.register_turn(session_id=f's{index}', thread_id=f't{index}',
                                  execute=execute, persist=None, cleanup=cleanup)
    await asyncio.gather(*(event.wait() for event in entered))
    task = asyncio.create_task(owner.cancel())
    await closing.wait()
    assert set(cleaned) == {0, 1}
    assert not owner.mux._completion.done()
    release.set()
    with pytest.raises(BaseExceptionGroup) as caught:
        await task
    assert set(leaves(caught.value)) == set(errors)
    assert cleaned[-1] == 'owner'
    assert (await owner.completion).outcome == 'failed'


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['aclose', 'aclosing', 'cancel'])
async def test_automatic_stream_close_preserves_control_flow(monkeypatch, boundary):
    from contextlib import aclosing
    from types import SimpleNamespace
    from voidx.bootstrap.autonomous_headless import _SDKLoopRun
    from voidx.sdk import VoidxAgent
    from voidx.config import Config

    error = OSError('cleanup secret')
    released = []
    entered = asyncio.Event()

    async def execute(supervisor):
        from uuid import uuid4
        from voidx.agent.domain.semantic_events import TurnStarted
        await supervisor.channel.publish(TurnStarted(
            **supervisor.channel.identity, event_id=uuid4(), sequence=1, timestamp=0.0,
            agent_id=None, parent_tool_call_id=None, payload={"text": "input", "metadata": {}},
        ))
        entered.set()
        await asyncio.Event().wait()

    async def cleanup(_):
        released.append('child')
        raise error

    owner = RunOwner()
    run = _SDKLoopRun(owner, 's',
                      SimpleNamespace(release=lambda: released.append('session')),
                      SimpleNamespace(close=lambda: released.append('workspace')))
    await owner.register_turn(session_id='s', thread_id='t', execute=execute,
                              persist=None, cleanup=cleanup)
    await entered.wait()

    async def build(*args, **kwargs):
        return run, run

    monkeypatch.setattr('voidx.sdk.agent.build_headless_run', build)
    agent = VoidxAgent(Config())
    stream = agent.stream('test')
    assert (await anext(stream)).kind == 'turn.started'
    if boundary == 'aclose':
        await stream.aclose()
    elif boundary == 'aclosing':
        async with aclosing(stream):
            pass
    else:
        waiting = asyncio.Event()

        async def consume():
            waiting.set()
            await anext(stream)

        task = asyncio.create_task(consume())
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert 'secret' not in ''.join(traceback.format_exception(caught.value))
    assert released == ['child', 'workspace', 'session']
    assert owner.producer_count == 0
    assert not agent._active
    assert (await owner.completion).outcome == 'failed'
    with pytest.raises(BaseExceptionGroup) as explicit:
        await run.cancel()
    assert leaves(explicit.value) == [error]
