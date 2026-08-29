"""Incremental gateway protocol regression tests."""

from __future__ import annotations

import json

import pytest

from voidx.presentation.gateway.session import GatewaySession
from voidx.presentation.output.dock import BottomInputDock
from voidx.presentation.output.events.schema import (
    AssistantStreamCommitted,
    AssistantStreamStarted,
    AssistantStreamUpdated,
    TurnCompleted,
)
from voidx.presentation.protocol.v2.incremental import (
    CAPABILITY_STREAM_APPEND,
    CAPABILITY_WORKSPACE_PATCH,
)


class FakeClient:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_text(self, text: str, *, priority: bool = False) -> None:
        self.messages.append(text)


def _messages(client: FakeClient, method: str) -> list[dict]:
    return [
        json.loads(message)
        for message in client.messages
        if json.loads(message).get("method") == method
    ]


@pytest.mark.asyncio
async def test_stream_capability_falls_back_per_client_and_preserves_text() -> None:
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    modern = FakeClient()
    legacy = FakeClient()
    await session.connect(modern, capabilities=[CAPABILITY_STREAM_APPEND])
    await session.connect(legacy)

    await session.broadcast_event(AssistantStreamStarted(stream_id="s1"))
    await session.broadcast_event(
        AssistantStreamUpdated(stream_id="s1", text="hello", phase="text")
    )
    await session.broadcast_event(
        AssistantStreamUpdated(stream_id="s1", text="hello world", phase="text")
    )
    await session.broadcast_event(AssistantStreamCommitted(stream_id="s1"))

    modern_updates = [
        message
        for message in _messages(modern, "item.delta")
        if message["params"]["kind"] == "assistant_stream"
    ]
    legacy_updates = [
        message
        for message in _messages(legacy, "item.delta")
        if message["params"]["kind"] == "assistant_stream"
    ]
    assert modern_updates[-1]["params"]["data"] == {
        "op": "append",
        "base_revision": 1,
        "revision": 2,
        "text": " world",
        "phase": "text",
        "stream_id": "s1",
        "workspace_revision": 0,
    }
    assert legacy_updates[-1]["params"]["data"]["text"] == "hello world"
    assert "op" not in legacy_updates[-1]["params"]["data"]


@pytest.mark.asyncio
async def test_stream_replace_and_phase_switch_advance_revision_without_suffix() -> None:
    session = GatewaySession(lambda: BottomInputDock().tree, thread_id="t1")
    client = FakeClient()
    await session.connect(client, capabilities=[CAPABILITY_STREAM_APPEND])

    await session.broadcast_event(AssistantStreamStarted(stream_id="s1"))
    await session.broadcast_event(
        AssistantStreamUpdated(stream_id="s1", text="abcdef", phase="text")
    )
    await session.broadcast_event(
        AssistantStreamUpdated(stream_id="s1", text="abX", phase="text")
    )
    await session.broadcast_event(
        AssistantStreamUpdated(stream_id="s1", text="reason", phase="thinking")
    )

    updates = [
        message
        for message in _messages(client, "item.delta")
        if message["params"]["kind"] == "assistant_stream"
    ]
    assert updates[-2]["params"]["data"]["op"] == "replace"
    assert updates[-2]["params"]["data"]["base_revision"] == 1
    assert updates[-2]["params"]["data"]["revision"] == 2
    assert updates[-2]["params"]["data"]["text"] == "abX"
    assert updates[-1]["params"]["data"]["op"] == "replace"
    assert updates[-1]["params"]["data"]["base_revision"] == 2
    assert updates[-1]["params"]["data"]["revision"] == 3
    assert updates[-1]["params"]["data"]["text"] == "reason"


@pytest.mark.asyncio
async def test_workspace_patch_does_not_build_transcript_snapshot(monkeypatch) -> None:
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1")
    client = FakeClient()
    await session.connect(client, capabilities=[CAPABILITY_WORKSPACE_PATCH])

    def fail_snapshot(*args, **kwargs):
        raise AssertionError("workspace patch must not build transcript snapshot")

    monkeypatch.setattr(
        "voidx.presentation.gateway.session.core.tree_to_snapshot",
        fail_snapshot,
    )

    await session.broadcast_snapshot(sync_persisted=False)

    assert _messages(client, "workspace.patch")[-1]["params"]["revision"] == 1


@pytest.mark.asyncio
async def test_turn_terminal_event_uses_patch_for_incremental_client() -> None:
    session = GatewaySession(lambda: BottomInputDock().tree, thread_id="t1")
    client = FakeClient()
    await session.connect(client, capabilities=[CAPABILITY_WORKSPACE_PATCH])

    await session.broadcast_event(TurnCompleted(thread_id="t1"))

    assert _messages(client, "workspace.patch")
    assert not _messages(client, "workspace.snapshot")[1:]
