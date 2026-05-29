import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

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
async def test_start_session_auto_resumes_latest_for_workspace(tmp_path):
    workspace = str(tmp_path)
    older = await create_session(workspace=workspace)
    latest = await create_session(workspace=workspace)
    await update_title(latest.id, "Latest session")
    console = FakeConsole()
    try:
        selected = await _select_start_session(
            workspace=workspace,
            provider="anthropic",
            model="claude",
            resume=None,
            new_session=False,
            vconsole=console,
        )

        assert selected.id == latest.id
        assert any(latest.id in message for message in console.messages)
    finally:
        await delete_session(older.id)
        await delete_session(latest.id)


@pytest.mark.asyncio
async def test_start_session_new_flag_skips_auto_resume(tmp_path):
    workspace = str(tmp_path)
    existing = await create_session(workspace=workspace)
    console = FakeConsole()
    selected = None
    try:
        selected = await _select_start_session(
            workspace=workspace,
            provider="anthropic",
            model="claude",
            resume=None,
            new_session=True,
            vconsole=console,
        )

        assert selected.id != existing.id
        assert selected.workspace == workspace
    finally:
        await delete_session(existing.id)
        if selected is not None:
            await delete_session(selected.id)


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
