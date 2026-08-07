"""Tests for guidance fallback when submitting during a running turn.

When a session.submit arrives while a turn is already in progress, the
gateway should route the text as guidance (kind=guide) to the command
handler instead of raising ERR_TURN_IN_PROGRESS.

Slash commands (text starting with /) should be dispatched directly to
the command handler as-is, not routed as guidance.
"""
from __future__ import annotations

import pytest

from voidx.presentation.gateway.session import GatewaySession
from voidx.presentation.output.dock import BottomInputDock
from voidx.presentation.protocol.commands import UiSubmitCommand
from voidx.presentation.protocol.v2.envelope import ERR_TURN_IN_PROGRESS

from tests.test_presentation.gateway.helpers import FakeClient


@pytest.mark.asyncio
async def test_submit_during_running_turn_routes_as_guidance():
    dock = BottomInputDock()
    handled: list[dict] = []

    async def command_handler(command):
        if isinstance(command, UiSubmitCommand):
            handled.append({"kind": command.kind, "text": command.text})
        else:
            handled.append(dict(command) if isinstance(command, dict) else {"raw": str(command)})

    session = GatewaySession(
        lambda: dock.tree,
        thread_id="t1",
        command_handler=command_handler,
    )
    client = FakeClient()
    await session.connect(client)
    session._run_manager.mark_running("t1")

    await session.handle_command(UiSubmitCommand(text="keep going", thread_id="t1"))

    assert len(handled) == 1
    assert handled[0]["kind"] == "guide"
    assert handled[0]["text"] == "keep going"


@pytest.mark.asyncio
async def test_submit_when_idle_still_routes_as_submit():
    dock = BottomInputDock()
    handled: list[dict] = []

    async def command_handler(command):
        handled.append({"kind": command.kind, "text": command.text})

    session = GatewaySession(
        lambda: dock.tree,
        thread_id="t1",
        command_handler=command_handler,
    )
    client = FakeClient()
    await session.connect(client)

    await session.handle_command(UiSubmitCommand(text="hello", thread_id="t1"))

    assert len(handled) == 1
    assert handled[0]["kind"] == "submit"
    assert handled[0]["text"] == "hello"


@pytest.mark.asyncio
async def test_submit_during_running_turn_does_not_raise():
    dock = BottomInputDock()

    async def command_handler(command):
        pass

    session = GatewaySession(
        lambda: dock.tree,
        thread_id="t1",
        command_handler=command_handler,
    )
    client = FakeClient()
    await session.connect(client)
    session._run_manager.mark_running("t1")

    await session.handle_command(UiSubmitCommand(text="guidance text", thread_id="t1"))


@pytest.mark.asyncio
async def test_slash_command_during_running_turn_dispatched_not_guidance():
    """Slash commands should be dispatched directly, not routed as guidance."""
    dock = BottomInputDock()
    handled: list[dict] = []

    async def command_handler(command):
        if isinstance(command, UiSubmitCommand):
            handled.append({"kind": command.kind, "text": command.text})
        else:
            handled.append(dict(command) if isinstance(command, dict) else {"raw": str(command)})

    session = GatewaySession(
        lambda: dock.tree,
        thread_id="t1",
        command_handler=command_handler,
    )
    client = FakeClient()
    await session.connect(client)
    session._run_manager.mark_running("t1")

    await session.handle_command(UiSubmitCommand(text="/model switch deepseek/deepseek-v4-pro --local", thread_id="t1"))

    assert len(handled) == 1
    assert handled[0]["kind"] == "submit"
    assert handled[0]["text"] == "/model switch deepseek/deepseek-v4-pro --local"
