from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from tests.langgraph_execution import make_langgraph_execution
from tests.test_agent.adapters.langgraph.runtime.stream_llm_helpers import FakeRenderer, TrackingStreamingModel
from tests.test_agent.adapters.langgraph.runtime.test_current_task_state_refresh import _install_old_builder
from voidx.agent.domain.task.state import GoalSpec, TaskState
from voidx.config import Config
from voidx.llm.domain.model import ModelConfig


def wire(messages):
    return [(m.type, m.content, getattr(m, 'tool_calls', None), getattr(m, 'tool_call_id', None)) for m in messages]


@pytest.mark.asyncio
async def test_retains_sent_task_state_through_tools_and_new_user_message(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as module
    monkeypatch.setattr(module, 'StreamingRenderer', FakeRenderer)
    graph = make_langgraph_execution(Config(model=ModelConfig(provider='openai', model='gpt-4o'), workspace=str(tmp_path)), api_key='test-key')
    graph.model = TrackingStreamingModel()
    _install_old_builder(graph, tmp_path)
    assert not hasattr(graph, "task_state_strip_enabled")
    messages = [HumanMessage(content='work', id='u')]

    async def call(goal):
        await graph._call_llm({'messages': list(messages), 'step_count': 1, 'persona': 'implement', 'turn_state': 'running', 'task_state': TaskState(current_goal=GoalSpec(desc=goal)).model_dump(mode='json')})
        return list(graph.model.messages)

    first = await call('first goal')
    messages += [AIMessage(content='', tool_calls=[{'name': 'read_file', 'args': {}, 'id': 'call_read', 'type': 'tool_call'}]), ToolMessage(content='result', tool_call_id='call_read')]
    second = await call('second goal')
    assert wire(second[:len(first)]) == wire(first)
    assert sum(str(m.content).count('## Current Task State') for m in second) == 2
    messages += [AIMessage(content='done'), HumanMessage(content='continue')]
    third = await call('third goal')
    assert wire(third[:len(second)]) == wire(second)
    assert sum(str(m.content).count('## Current Task State') for m in third) == 3
    fourth = await call('third goal')
    assert wire(fourth) == wire(third)
    assert graph.task_state_reminder_policy.state.calls_since_snapshot == 1


def test_retained_history_is_reset_between_sessions(tmp_path):
    graph = make_langgraph_execution(Config(workspace=str(tmp_path)), api_key='test-key')
    graph._session = SimpleNamespace(id='one')
    first = graph.task_state_history
    assert graph.task_state_history is first
    graph._session = SimpleNamespace(id='two')
    assert graph.task_state_history is not first
    second = graph.task_state_history
    graph.config.model.model = "different-model"
    assert graph.task_state_history is second


@pytest.mark.asyncio
async def test_failed_provider_request_does_not_add_a_retained_snapshot(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as module

    class RetryModel(TrackingStreamingModel):
        def __init__(self):
            super().__init__()
            self.attempts = []

        async def astream(self, messages):
            self.attempts.append(list(messages))
            if len(self.attempts) == 1:
                error = RuntimeError('context length exceeded')
                error.status_code = 400
                raise error
            async for chunk in super().astream(messages):
                yield chunk

    monkeypatch.setattr(module, 'StreamingRenderer', FakeRenderer)
    graph = make_langgraph_execution(Config(model=ModelConfig(provider='openai', model='gpt-4o'), workspace=str(tmp_path)), api_key='test-key')
    graph.model = RetryModel()
    graph._compaction.is_overflow = lambda _tokens: False
    _install_old_builder(graph, tmp_path)

    async def no_compaction(*_args, **_kwargs):
        return None, None

    graph._preflight_compact_if_needed = no_compaction
    await graph._call_llm({'messages': [HumanMessage(content='work')], 'step_count': 1, 'persona': 'implement', 'turn_state': 'running', 'task_state': TaskState(current_goal=GoalSpec(desc='goal')).model_dump(mode='json')})
    assert len(graph.model.attempts) == 2
    for attempt in graph.model.attempts:
        assert sum(str(m.content).count('## Current Task State') for m in attempt) == 1


@pytest.mark.asyncio
async def test_policy_uses_real_input_ids_and_full_semantic_request(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as module
    from voidx.agent.application.runtime_context import raw_semantic_messages

    monkeypatch.setattr(module, 'StreamingRenderer', FakeRenderer)
    graph = make_langgraph_execution(Config(model=ModelConfig(provider='openai', model='gpt-4o'), workspace=str(tmp_path)), api_key='test')
    graph.model = TrackingStreamingModel()
    _install_old_builder(graph, tmp_path)
    messages = [HumanMessage(content='work')]
    state = {'messages': messages, 'user_message_id': 1, 'step_count': 1, 'turn_state': 'running', 'task_state': TaskState(current_goal=GoalSpec(desc='goal')).model_dump(mode='json')}
    policy = graph.task_state_reminder_policy
    decisions = []
    prepare = policy.prepare

    def record(**kwargs):
        result = prepare(**kwargs)
        decisions.append(result)
        return result

    monkeypatch.setattr(policy, 'prepare', record)
    await graph._call_llm(state)
    messages.extend([AIMessage(content='answer'), HumanMessage(content='internal hint')])
    await graph._call_llm(state)
    assert decisions[-1].reason == 'unchanged'
    assert graph.task_state_history.snapshot_count == 1
    state['user_message_id'] = 2
    await graph._call_llm(state)
    assert decisions[-1].reason == 'user_message'
    graph._drain_pending_guidance = lambda: [(HumanMessage(content='supplement', additional_kwargs={'_voidx_guidance_event_id': 'event-1'}), False, 'user')]
    await graph._call_llm(state)
    assert decisions[-1].reason == 'user_message'
    assert decisions[-1].semantic_tokens == sum(module.estimate_message_tokens(m, graph.config.model.model) for m in raw_semantic_messages(graph.model.messages))
    await graph._call_llm(state)
    assert decisions[-1].reason == 'unchanged'


@pytest.mark.asyncio
async def test_repair_requests_commit_only_once_after_acceptance(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as module

    monkeypatch.setattr(module, 'StreamingRenderer', FakeRenderer)
    graph = make_langgraph_execution(Config(model=ModelConfig(provider='openai', model='gpt-4o'), workspace=str(tmp_path)), api_key='test')
    graph.model = TrackingStreamingModel()
    _install_old_builder(graph, tmp_path)
    requests = []
    accepted = AIMessage(content='done')
    malformed = AIMessage(content='broken')
    monkeypatch.setattr(module, 'is_malformed_tool_call_response', lambda message: message is malformed)

    async def stream(_model, messages, *_args, **_kwargs):
        assert graph.task_state_history.snapshot_count == 0
        assert graph.task_state_reminder_policy.state.snapshot is None
        requests.append(messages)
        return malformed if len(requests) == 1 else accepted

    monkeypatch.setattr(module, '_stream_llm', stream)
    await graph._call_llm({'messages': [HumanMessage(content='work')], 'step_count': 1, 'turn_state': 'running'})
    assert len(requests) == 2
    assert all(sum('## Current Task State' in str(m.content) for m in request) == 1 for request in requests)
    assert graph.task_state_history.snapshot_count == 1
    assert graph.task_state_reminder_policy.state.calls_since_snapshot == 0


@pytest.mark.asyncio
async def test_profile_suppression_preserves_container_without_injection(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as module

    monkeypatch.setattr(module, 'StreamingRenderer', FakeRenderer)
    graph = make_langgraph_execution(Config(model=ModelConfig(provider='openai', model='gpt-4o'), workspace=str(tmp_path)), api_key='test')
    graph.model = TrackingStreamingModel()
    _install_old_builder(graph, tmp_path)
    state = {'messages': [HumanMessage(content='work')], 'step_count': 1, 'turn_state': 'running'}
    await graph._call_llm(state)
    graph._last_context_builder._suppress_sections.add('Current Task State')
    await graph._call_llm(state)
    assert graph.task_state_history.snapshot_count == 1
    assert graph.task_state_reminder_policy.state.snapshot is None
    assert not any('## Current Task State' in str(m.content) for m in graph.model.messages)
    graph._last_context_builder._suppress_sections.clear()
    await graph._call_llm(state)
    assert graph.task_state_history.snapshot_count == 2


@pytest.fixture
def reminder_request(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as module

    monkeypatch.setattr(module, 'StreamingRenderer', FakeRenderer)
    graph = make_langgraph_execution(Config(model=ModelConfig(provider='openai', model='gpt-4o'), workspace=str(tmp_path)), api_key="test")
    graph.model = TrackingStreamingModel()
    _install_old_builder(graph, tmp_path)
    state = {'messages': [HumanMessage(content='work')], 'step_count': 1, 'turn_state': 'running'}
    return graph, state


def snapshots(messages):
    return [m for m in messages if '## Current Task State' in str(m.content)]


def reminder_log(caplog):
    return [r.getMessage() for r in caplog.records if 'task-state reminder reason=' in r.getMessage()][-1]


@pytest.mark.asyncio
async def test_default_reminder_appends_on_seventh_actual_request(reminder_request, caplog):
    graph, state = reminder_request
    caplog.set_level('DEBUG', logger='voidx.agent.adapters.langgraph.runtime.llm_turn')
    await graph._call_llm(state)
    first = wire(graph.model.messages)
    for count in range(1, 6):
        await graph._call_llm(state)
        assert wire(graph.model.messages) == first
        assert graph.task_state_reminder_policy.state.calls_since_snapshot == count
    assert 'append=False' in reminder_log(caplog)
    await graph._call_llm(state)
    assert len(snapshots(graph.model.messages)) == 2
    assert wire(graph.model.messages[:len(first)]) == first
    assert graph.task_state_history.snapshot_count == 2
    assert graph.task_state_reminder_policy.state.calls_since_snapshot == 0
    log = reminder_log(caplog)
    for field in ('reason=call_interval', 'session_id=None', 'agent_run_id=None', 'append=True', 'snapshot_count=2', 'anchor_reset=False', 'fingerprint=', 'calls=0', 'delta_tokens=', 'snapshot_tokens='):
        assert field in log


@pytest.mark.asyncio
@pytest.mark.parametrize('tokens, expected_count', [(8191, 1), (8192, 2)])
async def test_large_tool_result_semantic_token_boundary(reminder_request, monkeypatch, caplog, tokens, expected_count):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as module
    from voidx.agent.application.runtime_context import raw_semantic_messages

    graph, state = reminder_request
    caplog.set_level('DEBUG', logger=module.__name__)
    monkeypatch.setattr(module, 'estimate_message_tokens', lambda message, _model: len(message.content) if isinstance(message, ToolMessage) else 0)
    await graph._call_llm(state)
    first = wire(graph.model.messages)
    state['messages'].extend([
        AIMessage(content='', tool_calls=[{'name': 'read_file', 'args': {}, 'id': 'large', 'type': 'tool_call'}]),
        ToolMessage(content='x' * tokens, tool_call_id='large'),
    ])
    await graph._call_llm(state)
    assert sum(module.estimate_message_tokens(m, graph.config.model.model) for m in raw_semantic_messages(graph.model.messages)) == tokens
    assert wire(graph.model.messages[:len(first)]) == first
    assert len(snapshots(graph.model.messages)) == expected_count
    assert graph.task_state_history.snapshot_count == expected_count
    assert f'delta_tokens={tokens}' in reminder_log(caplog)
    assert f'reason={"token_interval" if tokens == 8192 else "unchanged"}' in reminder_log(caplog)


@pytest.mark.asyncio
@pytest.mark.parametrize('reset', ['compaction', 'model'])
async def test_reset_actual_request_never_restores_old_snapshots(reminder_request, caplog, reset):
    graph, state = reminder_request
    caplog.set_level('DEBUG', logger='voidx.agent.adapters.langgraph.runtime.llm_turn')
    for goal in ('obsolete one', 'obsolete two'):
        state['task_state'] = TaskState(current_goal=GoalSpec(desc=goal)).model_dump(mode='json')
        await graph._call_llm(state)
    assert graph.task_state_history.snapshot_count == 2
    if reset == 'compaction':
        graph._mark_task_state_compaction_applied()
        state['messages'] = [HumanMessage(content='rewritten history with more content than before')]
    else:
        graph.config.model.model = 'different-model'
    state['task_state'] = TaskState(current_goal=GoalSpec(desc='fresh goal')).model_dump(mode='json')
    await graph._call_llm(state)
    request = wire(graph.model.messages)
    assert len(snapshots(graph.model.messages)) == (1 if reset == "compaction" else 3)
    assert 'fresh goal' in str(request)
    assert ('obsolete' not in str(request)) == (reset == 'compaction')
    assert graph.task_state_history.snapshot_count == (1 if reset == "compaction" else 3)
    assert graph.task_state_reminder_policy.state.calls_since_snapshot == 0
    log = reminder_log(caplog)
    assert f'reason={"history_reset" if reset == "compaction" else "initial"}' in log
    assert f'anchor_reset={reset == "compaction"}' in log
    assert f'snapshot_count={1 if reset == "compaction" else 3}' in log
    await graph._call_llm(state)
    assert wire(graph.model.messages) == request
    assert graph.task_state_reminder_policy.state.calls_since_snapshot == 1


@pytest.mark.asyncio
async def test_reminder_diagnostic_identifies_session_and_root_run(reminder_request, caplog, tmp_path):
    from voidx.agent.adapters.persistence.session_repository import create_session

    graph, state = reminder_request
    graph._session = await create_session(workspace=str(tmp_path))
    graph.agent_gateway = SimpleNamespace(
        ensure_root=lambda session_id: f'run-for-{session_id}',
        list_child_runs=lambda _run_id: [],
    )
    caplog.set_level('DEBUG', logger='voidx.agent.adapters.langgraph.runtime.llm_turn')
    await graph._call_llm(state)
    log = reminder_log(caplog)
    assert f'session_id={graph._session.id}' in log
    assert f'agent_run_id=run-for-{graph._session.id}' in log
    assert 'append=True' in log
    assert 'snapshot_count=1' in log


@pytest.mark.asyncio
@pytest.mark.parametrize('trim', ['body', 'args'])
async def test_trimming_preserves_all_snapshot_positions(reminder_request, trim):
    graph, state = reminder_request
    state['messages'] += [
        AIMessage(content='', tool_calls=[{'name': 'read', 'args': {'file_path': 'x' * 1000}, 'id': 'trim', 'type': 'tool_call'}]),
        ToolMessage(content='result ' * 1000, tool_call_id='trim'),
    ]
    for goal in ('one', 'two', 'three'):
        state['task_state'] = TaskState(current_goal=GoalSpec(desc=goal)).model_dump(mode='json')
        await graph._call_llm(state)
    before = [(i, m.content) for i, m in enumerate(graph.model.messages) if m.additional_kwargs.get('_voidx_task_state_snapshot')]
    if trim == 'body':
        state['messages'][-1].content = 'short'
    else:
        state['messages'][-2].tool_calls[0]['args'] = {'file_path': 'x'}
    await graph._call_llm(state)
    after = [(i, m.content) for i, m in enumerate(graph.model.messages) if m.additional_kwargs.get('_voidx_task_state_snapshot')]
    assert after == before
    assert graph.task_state_history.snapshot_count == 3
    assert graph.task_state_reminder_policy.state.calls_since_snapshot == 1


@pytest.mark.asyncio
async def test_explicit_reset_survives_failed_call_until_accepted_commit(reminder_request, monkeypatch):
    graph, state = reminder_request
    await graph._call_llm(state)
    graph._mark_task_state_compaction_applied()
    original = graph.model

    class BrokenModel(TrackingStreamingModel):
        async def astream(self, messages):
            raise RuntimeError('provider unavailable')
            yield

    graph.model = BrokenModel()
    try:
        await graph._call_llm(state)
    except RuntimeError:
        pass
    assert graph._task_state_history_reset_pending
    assert graph.task_state_history.snapshot_count == 0
    graph.model = original
    await graph._call_llm(state)
    assert not graph._task_state_history_reset_pending
    assert graph.task_state_history.snapshot_count == 1
