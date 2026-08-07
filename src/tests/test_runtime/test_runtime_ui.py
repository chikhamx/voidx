from types import SimpleNamespace

import pytest

from voidx.presentation.runtime_port import PresentationUiAdapter
from voidx.presentation.terminal import frontend_factory


def test_presentation_ui_adapter_uses_instance_collaborators():
    output = SimpleNamespace(console=object())
    dock = SimpleNamespace(active=True)
    events = SimpleNamespace(is_running=True)
    tracker = object()
    adapter = PresentationUiAdapter(
        output=output,
        dock=dock,
        events=events,
        session_tracker=tracker,
    )

    assert adapter.ui is output
    assert adapter.console is output.console
    assert adapter.dock is dock
    assert adapter.events is events
    assert adapter.session_tracker is tracker
    assert adapter.get_dock() is dock
    assert adapter.via_events() is True


def test_frontend_factory_registers_and_creates_frontend():
    created = []

    class FakeFrontend:
        def __init__(self, status, commands):
            self.status = status
            self.commands = commands
            created.append(self)

    frontend_factory.register_default_frontend(FakeFrontend)
    try:
        frontend = frontend_factory.create_frontend("status", [("/help", "Help")])
        assert frontend.status == "status"
        assert frontend.commands == [("/help", "Help")]
        assert created == [frontend]
    finally:
        frontend_factory.register_default_frontend(frontend_factory._default_tui_frontend_factory)


def test_frontend_factory_errors_without_registered_frontend():
    frontend_factory.reset_default_frontend()
    try:
        with pytest.raises(RuntimeError, match="No frontend registered"):
            frontend_factory.create_frontend("status", [])
    finally:
        frontend_factory.register_default_frontend(frontend_factory._default_tui_frontend_factory)
