"""Durable goal outcomes and cleanup through the real SDK execution stack."""
import asyncio

import pytest
from langchain_core.messages import AIMessage
from pydantic import PrivateAttr

from tests.test_sdk.test_headless_runtime import FileRoundTripModel


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['exhausted', 'stop', 'lock'])
async def test_goal_terminal_cleanup_precedes_run_completion(tmp_path, monkeypatch, failure):
    from voidx.sdk import VoidxAgent
    from voidx.config import Config, PermissionMode, Settings
    from voidx.llm.adapters import langchain_model_factory
    from voidx.agent.adapters.persistence.session_repository import create_session
    from voidx.agent.adapters.persistence.thread_repository import ThreadStore
    from voidx.tooling.domain.interaction import InteractionResponse

    class Model(FileRoundTripModel):
        _step: int = PrivateAttr(default=0)

        def _reply(self, messages):
            script = [
                ('goal_init', {'goal': 'Verify outcome', 'acceptance_condition': 'Evidence verified', 'max_attempts': 1}),
                ('goal_checkpoint', {'summary': 'Work', 'progress': 'meaningful', 'evidence': ['work']}),
                ('goal_decision', {'status': 'continue' if failure == 'exhausted' else 'finished',
                                   'summary': 'Evidence checked', 'progress': 'meaningful', 'evidence': ['work']}),
            ]
            if self._step >= len(script):
                return AIMessage(content='Done')
            name, args = script[self._step]
            self._step += 1
            return AIMessage(content='', tool_calls=[{'id': f'outcome-{self._step}', 'name': name, 'args': args}])

    model = Model()
    monkeypatch.setattr(langchain_model_factory, 'create_chat_model', lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, 'create_resolver_model', lambda *a, **kw: model)
    session = await create_session(workspace=str(tmp_path), profile='goal')
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.PROJECT_TRUSTED)
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, 'local-test-not-a-credential')
    agent = VoidxAgent(config, settings=settings)
    events, errors, observed = [], [], []
    entered, release = asyncio.Event(), asyncio.Event()
    run = None

    async def consume():
        nonlocal run
        try:
            async for event in agent.stream('Verify outcome', session_id=session.id, workspace=str(tmp_path)):
                events.append(event)
                run = agent._run
                if event.kind == 'interaction.required':
                    original_stop = run.assembly.goal_service.stop
                    original_close = run._workspace_lock.close

                    async def stop(parent):
                        await original_stop(parent)
                        observed.append(run.owner.completion.done())
                        if failure == 'stop':
                            entered.set()
                            await release.wait()
                            raise OSError('injected durable stop failure')

                    def close():
                        original_close()
                        if failure == 'lock':
                            raise OSError('injected lock close failure')

                    monkeypatch.setattr(run.assembly.goal_service, 'stop', stop)
                    monkeypatch.setattr(run._workspace_lock, 'close', close)
                    request = event.payload.request
                    await agent.submit_interaction(request.interaction_id, InteractionResponse(
                        session_id=request.session_id, thread_id=request.thread_id,
                        turn_id=request.turn_id, value='approved'))
        except Exception as exc:
            errors.append(exc)

    async with asyncio.timeout(30):
        consumer = asyncio.create_task(consume())
        if failure == 'stop':
            await entered.wait()
            pending_during_close = not run.owner.completion.done()
            release.set()
        await consumer
    bindings = await ThreadStore().list_goal_generations(session.id)
    loaded = await ThreadStore().load(bindings[-1].goal_thread_id)
    assert loaded.state.lifecycle.value == ('blocked' if failure == 'exhausted' else 'completed')
    assert loaded.state.context['goal_run']['attempt_count'] == 1
    terminals = [e for e in events if e.kind in ('turn.completed', 'turn.failed', 'turn.cancelled')]
    assert len(terminals) == len({e.turn_id for e in terminals}) == 3
    assert all(e.kind == 'turn.completed' for e in terminals)
    assert (await run.owner.completion).outcome == 'failed'
    assert errors
    assert observed == [False]
    if failure == 'stop':
        assert pending_during_close
    assert run._session_lock._handle is None
    assert run._workspace_lock._closed
    assert run.owner.producer_count == 0
    assert await ThreadStore().list_pending_outbox(loaded.thread.thread_id) == []
