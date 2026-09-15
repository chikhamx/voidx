"""Real execution construction and TurnRunner without a presentation capability."""
import pytest

from voidx.agent.adapters.langgraph.execution import LangGraphExecution
from voidx.agent.adapters.langgraph.runtime.semantic_output import SemanticOutput
from voidx.agent.ports.presentation import NullPresentationSnapshotPort
from voidx.agent.ports.workspace_lock import NullWorkspaceWriteLock
from voidx.config import Config
from voidx.llm.domain.model import ReasoningEffort
from voidx.llm.domain.provider import get_context_limit
from voidx.skills.application.api import SkillsApi
from voidx.skills.registry import SkillRegistry
from voidx.skills.service import SkillService
from voidx.tooling.adapters.permission.in_memory_state import create_permission_service
from voidx.bootstrap.tooling import build_tool_registry, bind_scoped_tools, scoped_tool_registry


class Publisher:
    def __init__(self):
        self.events = []

    async def publish(self, event):
        self.events.append(event)


def make_execution(tmp_path, **overrides):
    publisher = Publisher()
    skills = SkillsApi(SkillService(SkillRegistry(str(tmp_path))))
    def permission(config, *, settings, notifier):
        return create_permission_service(permission_mode=config.permission_mode.value, notifier=notifier)
    options = dict(
        ui=None, workspace_write_lock=NullWorkspaceWriteLock(),
        semantic_output=SemanticOutput(publisher, session_id='session', thread_id='thread', turn_id='turn'),
        permission_notifier=lambda text: publisher.events.append(text),
        skills_api=skills, skills_api_provider=lambda workspace: skills,
        permission_service_factory=permission,
        model_factory=lambda *args: None, resolver_model_factory=lambda *args: None,
        tool_registry_factory=build_tool_registry, scoped_tools_binder=bind_scoped_tools,
        profile_tool_registry_factory=scoped_tool_registry, slash_handler_factory=lambda host: None,
        reasoning_effort_type=ReasoningEffort, context_limit_resolver=get_context_limit,
        provider_specs={}, language_labels={}, tone_labels={},
    )
    options.update(overrides)
    return LangGraphExecution(Config(workspace=str(tmp_path)), api_key=None, **options), publisher


def test_construct_real_execution_without_ui(tmp_path):
    execution, publisher = make_execution(tmp_path)
    assert execution._ui is None
    assert execution.graph is not None
    assert execution.update_service is None
    assert execution.clipboard_image is None
    assert isinstance(execution._session_runtime.presentation_snapshots, NullPresentationSnapshotPort)


@pytest.mark.parametrize('missing', ['semantic_output', 'permission_notifier'])
def test_headless_requires_business_output_ports(tmp_path, missing):
    with pytest.raises(ValueError, match=missing):
        make_execution(tmp_path, **{missing: None})


@pytest.mark.asyncio
async def test_real_turn_runner_without_ui(tmp_path):
    from langchain_core.messages import AIMessage
    from tests.test_sdk.test_headless_runtime import FileRoundTripModel
    from voidx.agent.domain.turn_context import TurnExecutionContext

    class ReplyModel(FileRoundTripModel):
        def _reply(self, messages):
            return AIMessage(content='HEADLESS_REPLY')

    execution, publisher = make_execution(tmp_path)
    execution.model = ReplyModel()
    await execution._turn_runner.run_once(
        'hello', context=TurnExecutionContext(
            session_id='session', thread_id='thread', workspace=str(tmp_path),
        ),
    )
    assert execution._ui is None
    assert execution._turn_runner.idle_event.is_set()
    assert any(getattr(event.payload, 'text', '') == 'HEADLESS_REPLY' for event in publisher.events)
    assert any(event.kind == 'turn.completed' for event in publisher.events)


@pytest.mark.asyncio
@pytest.mark.parametrize('cancelled', [False, True])
async def test_headless_runner_reports_failure_and_releases_idle(tmp_path, monkeypatch, cancelled):
    import asyncio
    from voidx.agent.domain.turn_context import TurnExecutionContext

    execution, publisher = make_execution(tmp_path)
    error = asyncio.CancelledError() if cancelled else RuntimeError('graph failed')

    async def fail(*args, **kwargs):
        raise error
        yield

    monkeypatch.setattr(execution.graph, 'astream', fail)
    with pytest.raises(type(error)) as raised:
        await execution._turn_runner.run_once('hello', context=TurnExecutionContext(
            session_id='session', thread_id='thread', workspace=str(tmp_path),
        ))
    assert raised.value is error
    assert execution._turn_runner.idle_event.is_set()
    assert publisher.events[-1].kind == ('turn.cancelled' if cancelled else 'turn.failed')


@pytest.mark.asyncio
async def test_headless_session_runtime_reuses_persistence_and_null_snapshot(tmp_path):
    from voidx.agent.adapters.persistence.session_repository import create_session
    from voidx.agent.domain.task.intent import InteractionMode

    session = await create_session(workspace=str(tmp_path))
    execution, _ = make_execution(tmp_path, session=session)
    execution._interaction_mode = InteractionMode.PLAN
    execution._session_date = '2026-09-13'
    await execution._session_runtime.persist_runtime_state()
    await execution._session_runtime.persist_transcript_snapshot()

    restored, _ = make_execution(tmp_path, session=session)
    await restored._session_runtime.restore_runtime_state()
    assert restored._interaction_mode == InteractionMode.PLAN
    assert restored._session_date == '2026-09-13'
    assert not await restored._session_runtime.restore_transcript_snapshot()
    assert not list(tmp_path.rglob('transcript.jsonl'))


@pytest.mark.asyncio
async def test_headless_recursion_fallback_is_published(tmp_path, monkeypatch):
    from langgraph.errors import GraphRecursionError
    from voidx.agent.domain.turn_context import TurnExecutionContext

    execution, publisher = make_execution(tmp_path)
    async def exhausted(*args, **kwargs):
        raise GraphRecursionError('limit')
        yield
    monkeypatch.setattr(execution.graph, 'astream', exhausted)
    await execution._turn_runner.run_once('hello', context=TurnExecutionContext(
        session_id='session', thread_id='thread', workspace=str(tmp_path),
    ))
    assert any(event.kind == 'assistant.committed' and event.payload.text for event in publisher.events)
    assert publisher.events[-1].kind == 'turn.completed'
