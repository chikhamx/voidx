from voidx.agent.application.agent_service import AgentService
from voidx.agent.application.turn_service import TurnService
from voidx.agent.composition import build_agent_app
from voidx.agent.facade import AgentFacade
from voidx.agent.infrastructure.langgraph.adapter import LangGraphTurnEngine
from voidx.agent.infrastructure.memory_session import MemorySessionAdapter
from voidx.agent.infrastructure.null_events import NullEventPublisher


def test_build_agent_app_wires_application_turn_service(monkeypatch):
    execution = object()
    monkeypatch.setattr(
        "voidx.agent.composition.LangGraphExecution",
        lambda config, api_key, *, session, settings: execution,
    )

    app = build_agent_app(object(), "key")

    assert isinstance(app, AgentFacade)
    assert isinstance(app._execution, AgentService)
    assert app._execution._execution is execution
    turns = app._execution._turns
    assert isinstance(turns, TurnService)
    assert isinstance(turns._engine, LangGraphTurnEngine)
    assert isinstance(turns._sessions, MemorySessionAdapter)
    assert isinstance(turns._events, NullEventPublisher)
