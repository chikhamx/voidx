from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from voidx.presentation.slash import SlashHandler
from tests.presentation_ui import make_presentation_ui


class FakeSessionRepository:
    def __init__(self) -> None:
        self.list_calls = 0

    async def list_sessions(self, limit: int = 50):
        self.list_calls += 1
        return []


class FakeSessionCleanup:
    async def plan_session_delete(self, scope: str):
        raise AssertionError("not used")

    async def apply_session_delete_plan(self, plan):
        raise AssertionError("not used")


@pytest.mark.asyncio
async def test_session_commands_use_injected_repository() -> None:
    repository = FakeSessionRepository()
    host = SimpleNamespace(app=None, workspace="/tmp", _ui=make_presentation_ui())
    handler = SlashHandler(
        host,
        session_repository=repository,
        session_cleanup=FakeSessionCleanup(),
    )

    assert await handler.dispatch("/session list") is True
    assert repository.list_calls == 1


def test_slash_model_and_session_do_not_import_persistence_adapters() -> None:
    commands_dir = Path(__file__).parents[2] / "voidx" / "presentation" / "slash" / "commands"

    for filename in ("model.py", "session.py"):
        source = (commands_dir / filename).read_text(encoding="utf-8")
        assert "voidx.agent.adapters.persistence" not in source
