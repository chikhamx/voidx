from voidx.agent.application.agent_service import AgentService
from voidx.agent.composition import build_agent_app
from voidx.agent.facade import AgentFacade
from voidx.agent.infrastructure.langgraph.adapter import LangGraphTurnEngine
from voidx.agent.infrastructure.memory_session import MemorySessionAdapter
from voidx.agent.infrastructure.null_events import NullEventPublisher
from voidx.agent.runtime import AgentRuntime


def test_build_agent_app_wires_runtime_entry(monkeypatch):
    execution = object()
    monkeypatch.setattr(
        "voidx.agent.composition.LangGraphExecution",
        lambda config, api_key, *, session, settings: execution,
    )

    app = build_agent_app(object(), "key")

    assert isinstance(app, AgentFacade)
    assert isinstance(app._execution, AgentService)
    assert app._execution._execution is execution
    runtime = app._execution._runtime
    assert isinstance(runtime, AgentRuntime)
    resources = runtime._resources
    assert isinstance(resources.turn_engine, LangGraphTurnEngine)
    assert isinstance(resources.sessions, MemorySessionAdapter)
    assert isinstance(resources.events, NullEventPublisher)
