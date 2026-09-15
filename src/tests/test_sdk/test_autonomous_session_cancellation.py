"""Cancellation through SDK, real LangGraph/tools/storage, scripted provider only."""
import asyncio

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from pydantic import PrivateAttr

from tests.test_sdk.test_headless_runtime import FileRoundTripModel


@pytest.mark.asyncio
@pytest.mark.parametrize('profile', ['goal', 'loop'])
@pytest.mark.parametrize('stage', ['approval', 'active', 'waiting', 'paused', 'full'])
async def test_sdk_autonomous_cancel_matrix(tmp_path, monkeypatch, profile, stage):
    from voidx.sdk import VoidxAgent
    from voidx.config import Config, PermissionMode, Settings
    from voidx.llm.adapters import langchain_model_factory
    from voidx.agent.adapters.persistence.session_repository import create_session
    from voidx.agent.adapters.persistence.thread_repository import ThreadStore
    from voidx.agent.adapters.persistence.headless_locks import HeadlessFileLock
    from voidx.agent.application.runtime.run_ownership import RunOwner
    from voidx.tooling.domain.interaction import InteractionResponse

    entered = asyncio.Event()
    unblock = asyncio.Event()

    class Model(FileRoundTripModel):
        _step: int = PrivateAttr(default=0)

        async def _astream(self, messages, **kwargs):
            script = ([
                ('goal_init', {'goal': 'Cancel safely', 'acceptance_condition': 'verified'}),
                ('goal_checkpoint', {'summary': 'Work', 'progress': 'meaningful', 'evidence': ['work']}),
            ] if profile == 'goal' else [
                ('loop_init', {'goal': 'Cancel safely'}),
                (None, 'Ready'),
                ('loop_start', {'goal': 'Work'}),
                ('loop_commit', {'outcome': 'continue', 'summary': 'Work', 'progress': 'meaningful', 'next_delay_seconds': 60}),
            ])
            block_at = (1 if profile == 'goal' else 2) if stage == 'active' else len(script)
            if self._step == block_at:
                entered.set()
                await unblock.wait()
            if self._step >= len(script):
                reply = AIMessage(content='Waiting')
            else:
                name, args = script[self._step]
                reply = AIMessage(content=args) if name is None else AIMessage(content='', tool_calls=[
                    {'id': f'cancel-{self._step}', 'name': name, 'args': args}])
            self._step += 1
            yield ChatGenerationChunk(message=AIMessageChunk(content=reply.content, tool_calls=reply.tool_calls))

    model = Model()
    monkeypatch.setattr(langchain_model_factory, 'create_chat_model', lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, 'create_resolver_model', lambda *a, **kw: model)
    if stage == 'full':
        original_init = RunOwner.__init__
        def small_queue(self, **kwargs):
            original_init(self, **{**kwargs, 'capacity': 1})
        monkeypatch.setattr(RunOwner, '__init__', small_queue)
    session = await create_session(workspace=str(tmp_path), profile=profile)
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.PROJECT_TRUSTED)
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, 'local-test-not-a-credential')
    events = []
    async with asyncio.timeout(20):
        async with VoidxAgent(config, settings=settings) as agent:
            stream = agent.stream('Cancel safely', session_id=session.id, workspace=str(tmp_path))
            async for event in stream:
                events.append(event)
                run = agent._run
                if stage in ('paused', 'full'):
                    if stage == 'full':
                        while run.owner.mux.queued < run.owner.mux.capacity:
                            await asyncio.sleep(.001)
                        assert run.owner.mux.queued == 1
                    break
                if event.kind == 'interaction.required':
                    request = event.payload.request
                    if stage == 'approval':
                        break
                    assert await agent.submit_interaction(request.interaction_id, InteractionResponse(
                        session_id=request.session_id, thread_id=request.thread_id,
                        turn_id=request.turn_id, value='approved'))
                if event.kind == 'turn.started' and event.session_id != session.id:
                    if stage == 'active' or (stage == 'waiting' and profile == 'goal' and len([
                        e for e in events if e.kind == 'turn.started' and e.session_id != session.id]) == 2):
                        await entered.wait()
                        break
                if stage == 'waiting' and profile == 'loop' and event.kind == 'turn.completed' and event.session_id != session.id:
                    break
            await asyncio.gather(agent.cancel(session_id=session.id), agent.cancel(session_id=session.id))
            events.extend([event async for event in stream])
    assert run._driver.done()
    assert run._completion_task is None or run._completion_task.done()
    assert run.owner.producer_count == run.owner.mux.registered == 0
    assert run._interactions == {}
    assert run._session_lock._handle is None
    assert run._workspace_lock._closed and run._workspace_lock._lock._handle is None
    assert (await run.owner.completion).outcome == 'cancelled'
    if stage == 'approval':
        assert model._step == 1
        assert not any(e.kind == 'turn.started' and e.session_id != session.id for e in events)
        resolutions = [e.payload.resolution for e in events if e.kind == 'interaction.resolved']
        assert len(resolutions) == 1
        assert resolutions[0].decision != 'approved'
        assert resolutions[0].value != 'approved'
        assert resolutions[0].resolution_reason == 'task_cancelled'
        assert await ThreadStore().list_goal_generations(session.id) == []
        assert run.assembly.goal_service._active_specs == {}
        assert run.assembly.loop_service._active_specs == {}
    children = [e for e in events if e.kind == 'turn.started' and e.session_id != session.id]
    if stage in ('active', 'waiting'):
        assert children
        loaded = await ThreadStore().load(children[0].thread_id)
        assert loaded.state.lifecycle.value == 'cancelled'
        assert loaded.state.lifecycle_decision.outcome == 'stop'
        assert await ThreadStore().list_pending_outbox(children[0].thread_id) == []
    for event in children:
        probe = HeadlessFileLock('session', event.session_id)
        await asyncio.wait_for(probe.acquire(), 1)
        probe.release()
