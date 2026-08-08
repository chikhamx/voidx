from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def inject_slash_persistence_ports(monkeypatch):
    from voidx.agent.adapters.persistence import session_cleanup
    from voidx.agent.adapters.slash_host import build_slash_ports
    from voidx.agent.adapters.persistence.session_adapter import SessionRepositoryAdapter
    from voidx.presentation.slash.handler import SlashHandler
    from voidx.presentation.slash.handler import ui
    from tests.presentation_ui import make_presentation_ui
    from voidx.presentation.output.dock import BottomInputDock

    original_init = SlashHandler.__init__

    from types import SimpleNamespace

    defaults = {
        "ui": None,
        "api_key": None,
        "app": None,
        "clipboard_image": None,
        "config": SimpleNamespace(model=SimpleNamespace(provider="anthropic", model="claude-sonnet-4-6")),
        "goal_service": None,
        "loop_service": None,
        "lsp_manager": None,
        "mcp_manager": None,
        "model": None,
        "model_catalog": None,
        "permission": SimpleNamespace(permission_mode="safe"),
        "provider_specs": {},
        "reasoning_effort_type": None,
        "session": None,
        "settings": None,
        "skills_api": None,
        "update_service": None,
        "usage_stats": None,
        "workspace": "",
        "language_labels": {},
        "tone_labels": {},
    }

    def test_init(self, commands, *, session_repository=None, session_cleanup=None):
        if not hasattr(commands, "ui") or commands.ui is None:
            commands.ui = ui
        for name, value in defaults.items():
            if not hasattr(commands, name):
                setattr(commands, name, value)
        if not hasattr(commands, "presentation_ui") or commands.presentation_ui is None:
            commands.presentation_ui = getattr(commands, "_ui", None) or make_presentation_ui(dock=BottomInputDock())
        commands.presentation_ui.bind_frontend(commands.app)
        if not hasattr(commands, "model_factory") and hasattr(commands, "_model_factory"):
            commands.model_factory = commands._model_factory
        if not hasattr(commands, "compaction"):
            commands.compaction = getattr(commands, "_compaction", None)
        return original_init(
            self,
            *build_slash_ports(commands),
            session_repository=session_repository or SessionRepositoryAdapter(),
            session_cleanup=session_cleanup or session_cleanup_module,
        )

    session_cleanup_module = session_cleanup
    monkeypatch.setattr(SlashHandler, "__init__", test_init)
