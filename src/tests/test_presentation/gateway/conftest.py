"""Shared fixtures and helpers for test_ui/gateway tests."""

import re
import sys
from pathlib import Path

import pytest
from rich.text import Text


from voidx.presentation.output.dock import ANSI_LINE_PREFIX, BottomInputDock, set_dock
from voidx.presentation.output.events import DockEventConsumer, ui_events


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _plain(line: str) -> str:
    return _ANSI_RE.sub("", line.replace(ANSI_LINE_PREFIX, ""))


def _rich_plain(line: str) -> str:
    return Text.from_markup(_plain(line)).plain


def _tree_nodes(root):
    nodes = [root]
    for child in root.children:
        nodes.extend(_tree_nodes(child))
    return nodes


@pytest.fixture(autouse=True)
def configured_settings_factory(monkeypatch):
    from voidx.bootstrap.application import build_settings
    from voidx.agent.adapters.persistence.session_adapter import SessionRepositoryAdapter
    from voidx.presentation.gateway.session.core import GatewaySession

    original_init = GatewaySession.__init__

    def test_init(self, *args, settings_factory=None, session_repository=None, **kwargs):
        return original_init(
            self,
            *args,
            settings_factory=settings_factory or build_settings,
            session_repository=session_repository or SessionRepositoryAdapter(),
            **kwargs,
        )

    monkeypatch.setattr(GatewaySession, "__init__", test_init)


@pytest.fixture
def isolated_dock():
    test_dock = BottomInputDock()
    set_dock(test_dock)
    try:
        yield test_dock
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)


