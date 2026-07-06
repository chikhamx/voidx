import sys
from pathlib import Path


import pytest

from voidx.main import _select_start_session
from voidx.memory.session import create_session, delete_session, update_title


class FakeConsole:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.errors: list[str] = []

    def print(self, message: str) -> None:
        self.messages.append(message)

    def error(self, message: str) -> None:
        self.errors.append(message)


@pytest.mark.asyncio
async def test_start_session_default_returns_none():
    """Without --resume, session creation is deferred to first user input."""
    workspace = str(tmp_path) if (tmp_path := Path("/tmp/test_defer")) else "."
    console = FakeConsole()
    selected = await _select_start_session(
        workspace=workspace,
        provider="anthropic",
        model="claude",
        resume=None,
        new_session=False,
        vconsole=console,
    )

    assert selected is None


@pytest.mark.asyncio
async def test_start_session_new_flag_returns_none():
    """--new also defers session creation; no empty session is created."""
    workspace = str(tmp_path) if (tmp_path := Path("/tmp/test_new_defer")) else "."
    console = FakeConsole()
    selected = await _select_start_session(
        workspace=workspace,
        provider="anthropic",
        model="claude",
        resume=None,
        new_session=True,
        vconsole=console,
    )

    assert selected is None


@pytest.mark.asyncio
async def test_start_session_explicit_resume_overrides_workspace(tmp_path):
    workspace = str(tmp_path)
    other_workspace = str(tmp_path / "other")
    session = await create_session(workspace=other_workspace)
    console = FakeConsole()
    try:
        selected = await _select_start_session(
            workspace=workspace,
            provider="anthropic",
            model="claude",
            resume=session.id,
            new_session=False,
            vconsole=console,
        )

        assert selected.id == session.id
        assert selected.workspace == other_workspace
    finally:
        await delete_session(session.id)
