"""Real SDK child tools and HITL; only the provider responses are scripted."""
from __future__ import annotations

import asyncio
from collections import Counter

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import PrivateAttr

from tests.test_sdk.test_headless_runtime import FileRoundTripModel
from voidx.agent.adapters.persistence.session_repository import create_session
from voidx.agent.adapters.persistence.thread_repository import ThreadStore
from voidx.config import Config, PermissionMode, Settings
from voidx.llm.adapters import langchain_model_factory
from voidx.sdk import VoidxAgent
from voidx.tooling.domain.interaction import InteractionResponse


class ChildToolsModel(FileRoundTripModel):
    _step: int = PrivateAttr(default=0)

    def _reply(self, messages):
        self._histories.append(list(messages))
        script = [
            ('goal_init', {'goal': 'Verify file evidence', 'acceptance_condition': 'File contains second evidence', 'max_attempts': 3}),
            ('write', {'file_path': 'child-evidence.txt', 'op': 'write', 'new_string': 'first evidence\n'}),
            ('read', {'file_path': 'child-evidence.txt'}),
            ('goal_checkpoint', {'summary': 'First file written and read', 'progress': 'meaningful', 'evidence': ['child-evidence.txt: first evidence']}),
            ('read', {'file_path': 'child-evidence.txt'}),
            ('goal_decision', {'status': 'continue', 'summary': 'Read first evidence; second missing', 'progress': 'meaningful', 'next_hint': 'Write second evidence'}),
            (None, 'EVALUATED_FIRST: first evidence; continue.'),
            ('write', {'file_path': 'child-evidence.txt', 'op': 'write', 'new_string': 'second evidence\n'}),
            ('read', {'file_path': 'child-evidence.txt'}),
            ('goal_checkpoint', {'summary': 'Second file written and read', 'progress': 'meaningful', 'evidence': ['child-evidence.txt: second evidence']}),
            ('read', {'file_path': 'child-evidence.txt'}),
            ('goal_decision', {'status': 'finished', 'summary': 'Read second evidence verified', 'progress': 'meaningful', 'evidence': ['child-evidence.txt: second evidence']}),
            (None, 'EVALUATED_SECOND: second evidence; finished.'),
        ]
        if self._step >= len(script):
            return AIMessage(content='Script exhausted.')
        name, value = script[self._step]
        self._step += 1
        return AIMessage(content=value) if name is None else AIMessage(content='', tool_calls=[{
            'id': f'child-tools-{self._step}', 'name': name, 'args': value,
        }])


def identity(event):
    return event.session_id, event.thread_id, event.turn_id


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_child', [False, True])
async def test_sdk_goal_real_child_files_and_hitl(tmp_path, monkeypatch, cancel_child):
    model = ChildToolsModel()
    monkeypatch.setattr(langchain_model_factory, 'create_chat_model', lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, 'create_resolver_model', lambda *a, **kw: model)
    session = await create_session(workspace=str(tmp_path), profile='goal')
    config = Config(workspace=str(tmp_path), permission_mode=PermissionMode.SAFE)
    settings = Settings(str(tmp_path))
    settings.set_runtime_api_key(config.model.provider, 'local-test-not-a-credential')
    events, child_requests = [], []
    async with VoidxAgent(config, settings=settings) as agent:
        async with asyncio.timeout(30):
            async for event in agent.stream('Verify file evidence', session_id=session.id, workspace=str(tmp_path)):
                events.append(event)
                run = agent._run
                if event.kind != 'interaction.required':
                    continue
                request = event.payload.request
                assert identity(event) == (request.session_id, request.thread_id, request.turn_id)
                if request.purpose == 'goal':
                    assert identity(event) == identity(events[0])
                    value = 'approved'
                else:
                    assert request.purpose == 'permission'
                    assert request.tools[0].name in {'write', 'goal_checkpoint', 'goal_decision'}
                    child_requests.append(request)
                    assert request.session_id != session.id
                    assert request.thread_id != session.id
                    assert request.turn_id != events[0].turn_id
                    if request.tools[0].name == 'write':
                        if len(child_requests) == 1:
                            assert not (tmp_path / 'child-evidence.txt').exists()
                        else:
                            assert (tmp_path / 'child-evidence.txt').read_text() == 'first evidence\n'
                    coordinator = run._interactions[request.turn_id]
                    pending = coordinator._pending[request.interaction_id]
                    with pytest.raises(ValueError, match='ownership'):
                        await agent.submit_interaction(request.interaction_id, InteractionResponse(
                            session_id=session.id, thread_id=session.id,
                            turn_id=events[0].turn_id, value='allow'))
                    assert coordinator._pending[request.interaction_id] is pending
                    assert pending.resolution is None
                    if cancel_child:
                        await agent.cancel(session_id=session.id)
                        continue
                    value = 'allow'
                assert await agent.submit_interaction(request.interaction_id, InteractionResponse(
                    session_id=request.session_id, thread_id=request.thread_id,
                    turn_id=request.turn_id, value=value))
    assert len(child_requests) == (1 if cancel_child else 6), '\n'.join(
        f'{e.kind}: {e.payload}' for e in events if e.kind in {'tool.result', 'turn.failed', 'diagnostic.error'})
    started = [e for e in events if e.kind == 'turn.started']
    terminals = [e for e in events if e.kind in {'turn.completed', 'turn.failed', 'turn.cancelled'}]
    assert Counter(identity(e) for e in terminals) == Counter(identity(e) for e in started)
    assert all(count == 1 for count in Counter(identity(e) for e in terminals).values())
    assert len(started) == (2 if cancel_child else 5)
    for request in child_requests:
        resolved = [e for e in events if e.kind == 'interaction.resolved'
                    and e.payload.interaction_id == request.interaction_id]
        assert len(resolved) == 1
        assert identity(resolved[0]) == (request.session_id, request.thread_id, request.turn_id)
        resolution = resolved[0].payload.resolution
        assert resolution.resolution_reason == ('task_cancelled' if cancel_child else 'answered')
        assert resolution.value != 'approved'
        if cancel_child:
            assert resolution.decision == 'deny'
        else:
            assert resolution.decision == 'approved'
    loaded = await ThreadStore().load(child_requests[0].thread_id)
    assert loaded is not None
    assert loaded.state.lifecycle.value == ('cancelled' if cancel_child else 'completed')
    assert await ThreadStore().list_pending_outbox(loaded.thread.thread_id) == []
    assert (await run.owner.completion).outcome == ('cancelled' if cancel_child else 'completed')
    assert run.owner.producer_count == run.owner.mux.registered == 0
    assert run._interactions == {}
    assert run._driver.done()
    if cancel_child:
        assert not (tmp_path / 'child-evidence.txt').exists()
    else:
        assert (tmp_path / 'child-evidence.txt').read_text() == 'second evidence\n'
    tool_results = [e for e in events if e.kind == 'tool.result' and e.payload.name in {'read', 'write'}]
    assert len(tool_results) == (0 if cancel_child else 6)
    for event in tool_results:
        assert identity(event) in {identity(e) for e in started[1:]}
        assert event.payload.summary
    assert all(e.payload.ok for e in events if e.kind == 'tool.finished')
    messages = {m.tool_call_id: m for history in model._histories for m in history if isinstance(m, ToolMessage)}
    if not cancel_child:
        assert 'first evidence' in str(messages['child-tools-3'].content)
        assert 'first evidence' in str(messages['child-tools-5'].content)
        assert 'second evidence' in str(messages['child-tools-9'].content)
        assert 'second evidence' in str(messages['child-tools-11'].content)
        for index, marker in [(2, 'EVALUATED_FIRST'), (4, 'EVALUATED_SECOND')]:
            texts = [e for e in events if e.kind == 'assistant.committed' and marker in e.payload.text]
            assert len(texts) == 1
            assert identity(texts[0]) == identity(started[index])
        assert all(e.kind == 'turn.completed' for e in terminals)
        assert loaded.state.lifecycle_decision.outcome == 'completed'
        assert loaded.state.context['goal_run']['attempt_count'] == 2
        assert [r.tools[0].name for r in child_requests] == [
            'write', 'goal_checkpoint', 'goal_decision',
            'write', 'goal_checkpoint', 'goal_decision',
        ]
        expected_turns = [1, 1, 2, 3, 3, 4]
        assert [(r.session_id, r.thread_id, r.turn_id) for r in child_requests] == [
            identity(started[index]) for index in expected_turns
        ]
        assert [e.payload.tool_call_id for e in tool_results] == [
            'child-tools-2', 'child-tools-3', 'child-tools-5',
            'child-tools-8', 'child-tools-9', 'child-tools-11',
        ]
        assert [identity(e) for e in tool_results] == [
            identity(started[index]) for index in expected_turns
        ]
