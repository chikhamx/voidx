from types import SimpleNamespace

from voidx.agent.application.agent_service import AgentService
from voidx.agent.application.coding_service import CodingService
from voidx.agent.application.runtime import AgentRuntime
from voidx.bootstrap.agent import AgentResources, build_agent_components
from voidx.agent.facade import AgentFacade
from voidx.agent.adapters.langgraph.adapter import LangGraphTurnEngine
from voidx.agent.adapters.persistence.memory_session import MemorySessionAdapter
from voidx.agent.adapters.null_events import NullEventPublisher
from voidx.agent.ports.presentation import NullAgentEventPublisher
from voidx.presentation.terminal.events import UiAgentEventPublisher
from voidx.presentation.terminal.run_loop import TerminalRunLoop
from tests.presentation_ui import make_presentation_ui

runtime_ui_port = make_presentation_ui()


def test_build_agent_components_wires_presentation_neutral_runtime(monkeypatch):
    execution = SimpleNamespace(
        session=None,
        model=None,
        session_id="",
        workspace="",
        slash=SimpleNamespace(dispatch=lambda _command: False),
        bind_coding_turn_runner=lambda _runner: None,
        bind_automation_services=lambda _loop, _goal: None,
        can_submit_guidance=lambda: False,
        submit_guidance=lambda *_args, **_kwargs: False,
    )
    injected = {}

    def fake_execution(config, api_key, **kwargs):
        injected.update(kwargs)
        return execution

    monkeypatch.setattr("voidx.bootstrap.agent.LangGraphExecution", fake_execution)

    components = build_agent_components(SimpleNamespace(workspace=""), "key", ui=runtime_ui_port)

    assert isinstance(components, AgentResources)
    assert components.execution is execution
    assert injected["model_catalog"] is not None
    assert callable(injected["model_catalog_factory"])
    assert isinstance(components.service, AgentService)
    assert not hasattr(components.service, "_execution")
    assert isinstance(components.service._autonomous_router._events, NullAgentEventPublisher)
    runtime = components.service._autonomous_router._runtime
    assert isinstance(runtime, AgentRuntime)
    resources = runtime._resources
    assert isinstance(resources.turn_engine, LangGraphTurnEngine)
    assert isinstance(resources.sessions, MemorySessionAdapter)
    assert isinstance(resources.events, NullEventPublisher)
    assert isinstance(components.service._autonomous_router._coding_service, CodingService)


def test_bootstrap_build_agent_app_owns_terminal_composition(monkeypatch):
    from voidx.bootstrap.agent import build_agent_app
    from voidx.presentation.output.console import VoidConsole

    snapshots = []
    execution = SimpleNamespace(
        ui=VoidConsole(),
        bind_presentation_snapshots=snapshots.append,
        bind_startup_presenter=lambda _presenter: None,
        skills_api_provider=lambda _workspace: SimpleNamespace(service=object()),
    )
    service = object()
    captured = {}

    def fake_build_agent_components(config, api_key, **kwargs):
        captured.update(kwargs)
        return AgentResources(execution=execution, service=service)

    monkeypatch.setattr(
        "voidx.bootstrap.agent.build_agent_components",
        fake_build_agent_components,
    )

    app = build_agent_app(object(), "key")

    assert isinstance(app, AgentFacade)
    assert isinstance(app._run_loop, TerminalRunLoop)
    assert app._run_loop._status_reader._host is execution
    assert app._run_loop._sessions._host is execution
    assert app._run_loop._integrations._host is execution
    assert app._run_loop._frontend_binding._host is execution
    assert app._run_loop._ui is captured["ui"]
    assert app._run_loop._input_port is service
    publisher = captured["event_publisher_factory"](execution)
    assert isinstance(publisher, UiAgentEventPublisher)
    publisher.publish_message("hello")


async def test_agent_facade_run_delegates_to_run_loop():
    captured = {}

    class RunLoop:
        async def run(self, **kwargs):
            captured.update(kwargs)

    app = AgentFacade(run_loop=RunLoop())

    await app.run(web=True, web_headless=True, web_host="0.0.0.0", web_port=8787, web_token="token")

    assert captured == {
        "web": True,
        "web_headless": True,
        "web_host": "0.0.0.0",
        "web_port": 8787,
        "web_token": "token",
    }


def test_bootstrap_resource_types_are_explicit_and_factories_are_named_protocols():
    from typing import Any, get_type_hints

    from voidx.agent.adapters.langgraph.execution import LangGraphExecution
    from voidx.bootstrap import agent as bootstrap_agent

    agent_hints = get_type_hints(bootstrap_agent.AgentResources)
    assert agent_hints["execution"] is LangGraphExecution

    for resources_type in (
        bootstrap_agent.ApplicationResources,
        bootstrap_agent.IntegrationResources,
        bootstrap_agent.AgentResources,
    ):
        for annotation in get_type_hints(resources_type).values():
            assert annotation is not Any
            assert "typing.Any" not in str(annotation)
            assert "Callable[...," not in str(annotation)

    integration_hints = get_type_hints(bootstrap_agent.IntegrationResources)
    factory_types = {
        annotation
        for annotation in integration_hints.values()
        for annotation in getattr(annotation, "__args__", (annotation,))
        if annotation is not type(None)
    }
    assert factory_types
    assert all(getattr(factory_type, "_is_protocol", False) for factory_type in factory_types)

    event_call = bootstrap_agent.AgentEventPublisherFactory.__call__
    event_hints = get_type_hints(event_call)
    assert event_hints["execution"] is LangGraphExecution


def test_build_agent_components_rejects_missing_required_dependencies_at_entry(monkeypatch):
    def unexpected_execution(*_args, **_kwargs):
        raise AssertionError("execution construction must not start with missing dependencies")

    monkeypatch.setattr("voidx.bootstrap.agent.LangGraphExecution", unexpected_execution)

    import pytest

    with pytest.raises((TypeError, ValueError), match="config"):
        build_agent_components(None, "key", ui=runtime_ui_port)
    with pytest.raises((TypeError, ValueError), match="ui"):
        build_agent_components(SimpleNamespace(workspace=""), "key", ui=None)


def test_build_agent_components_keeps_product_integrations_optional(monkeypatch):
    execution = SimpleNamespace(
        session=None,
        model=None,
        session_id="",
        workspace="",
        slash=SimpleNamespace(dispatch=lambda _command: False),
        bind_coding_turn_runner=lambda _runner: None,
        bind_automation_services=lambda _loop, _goal: None,
        can_submit_guidance=lambda: False,
        submit_guidance=lambda *_args, **_kwargs: False,
    )
    injected = {}

    def fake_execution(config, api_key, **kwargs):
        injected.update(kwargs)
        return execution

    monkeypatch.setattr("voidx.bootstrap.agent.LangGraphExecution", fake_execution)

    build_agent_components(SimpleNamespace(workspace=""), "key", ui=runtime_ui_port)

    assert injected["external_manager_factory"] is None
    assert injected["mcp_reference_resolver"] is None
    assert injected["web_route"] is None
