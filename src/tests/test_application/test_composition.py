from types import SimpleNamespace

from voidx.agent.application.agent_service import AgentService
from voidx.agent.application.coding_service import CodingService
from voidx.agent.application.runtime import AgentRuntime
from voidx.agent.composition import AgentComponents, build_agent_components
from voidx.agent.facade import AgentFacade
from voidx.agent.infrastructure.langgraph.adapter import LangGraphTurnEngine
from voidx.agent.infrastructure.memory_session import MemorySessionAdapter
from voidx.agent.infrastructure.null_events import NullEventPublisher
from voidx.agent.ports.presentation import NullAgentEventPublisher
from voidx.presentation.terminal.events import UiAgentEventPublisher
from voidx.presentation.terminal.run_loop import TerminalRunLoop


def test_build_agent_components_wires_presentation_neutral_runtime(monkeypatch):
    execution = SimpleNamespace()
    monkeypatch.setattr(
        "voidx.agent.composition.LangGraphExecution",
        lambda config, api_key, *, session, settings, external_manager_factory, mcp_reference_resolver, web_route: execution,
    )

    components = build_agent_components(object(), "key")

    assert isinstance(components, AgentComponents)
    assert components.execution is execution
    assert isinstance(components.service, AgentService)
    assert components.service._execution is execution
    assert isinstance(components.service._events, NullAgentEventPublisher)
    runtime = components.service._runtime
    assert isinstance(runtime, AgentRuntime)
    resources = runtime._resources
    assert isinstance(resources.turn_engine, LangGraphTurnEngine)
    assert isinstance(resources.sessions, MemorySessionAdapter)
    assert isinstance(resources.events, NullEventPublisher)
    assert isinstance(components.service._coding_service, CodingService)


def test_bootstrap_build_agent_app_owns_terminal_composition(monkeypatch):
    from voidx.bootstrap.agent import build_agent_app

    snapshots = []
    execution = SimpleNamespace(
        ui=SimpleNamespace(),
        bind_presentation_snapshots=snapshots.append,
        bind_startup_presenter=lambda _presenter: None,
    )
    service = object()
    captured = {}

    def fake_build_agent_components(config, api_key, **kwargs):
        captured.update(kwargs)
        return AgentComponents(execution=execution, service=service)

    monkeypatch.setattr(
        "voidx.bootstrap.agent.build_agent_components",
        fake_build_agent_components,
    )

    app = build_agent_app(object(), "key")

    assert isinstance(app, AgentFacade)
    assert app._execution is service
    assert isinstance(app._run_loop, TerminalRunLoop)
    assert app._run_loop._execution is execution
    assert app._run_loop._service is service
    publisher = captured["event_publisher_factory"](execution)
    assert isinstance(publisher, UiAgentEventPublisher)


async def test_agent_facade_run_delegates_to_run_loop():
    captured = {}

    class RunLoop:
        async def run(self, **kwargs):
            captured.update(kwargs)

    app = AgentFacade(object(), run_loop=RunLoop())

    await app.run(web=True, web_headless=True, web_host="0.0.0.0", web_port=8787, web_token="token")

    assert captured == {
        "web": True,
        "web_headless": True,
        "web_host": "0.0.0.0",
        "web_port": 8787,
        "web_token": "token",
    }
