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
    assert graph.task_state_strip_enabled is True
    graph.set_task_state_strip(False)
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
    graph.set_task_state_strip(True)
    fourth = await call('fourth goal')
    assert sum(str(m.content).count('## Current Task State') for m in fourth) == 1
    assert 'first goal' not in str(wire(fourth))


def test_retained_history_is_reset_between_sessions(tmp_path):
    graph = make_langgraph_execution(Config(workspace=str(tmp_path)), api_key='test-key')
    graph.set_task_state_strip(False)
    graph._session = SimpleNamespace(id='one')
    first = graph.task_state_history
    assert graph.task_state_history is first
    graph._session = SimpleNamespace(id='two')
    assert graph.task_state_history is not first
    second = graph.task_state_history
    graph.set_task_state_strip(True)
    graph.set_task_state_strip(False)
    assert graph.task_state_history is not second


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
    graph.set_task_state_strip(False)
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
