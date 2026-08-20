"""Tests for persisted thread metadata registration in the web gateway."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from voidx.agent.ports.presentation import RuntimePresentationStatus, SessionPresentationStatus
from voidx.presentation.gateway.session_adapter import build_gateway_session
from voidx.presentation.gateway.thread_registry import GatewayThreadRegistryAdapter
from voidx.presentation.output.dock import BottomInputDock


class FakeGatewaySession:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def has_thread(self, thread_id: str) -> bool:
        return False

    async def register_thread(self, thread_id: str, **kwargs: str) -> None:
        self.calls.append({"thread_id": thread_id, **kwargs})


async def test_ensure_thread_preserves_runtime_profile() -> None:
    gateway_session = FakeGatewaySession()
    registry = GatewayThreadRegistryAdapter(lambda: gateway_session)

    registry.ensure_thread(
        SessionPresentationStatus(
            session_id="chat-session",
            title="Chat",
            directory="/workspace",
            runtime_profile="chat",
            is_new=False,
        )
    )
    await asyncio.sleep(0)

    assert gateway_session.calls == [
        {
            "thread_id": "chat-session",
            "title": "Chat",
            "directory": "/workspace",
            "runtime_profile": "chat",
            "profile_snapshot": None,
        }
    ]




def test_build_gateway_session_restores_initial_profile_snapshot(monkeypatch) -> None:
    snapshot = object()
    resolved = object()
    restored = []

    def restore(workspace, profile_id, value):
        restored.append((workspace, profile_id, value))
        return resolved

    monkeypatch.setattr("voidx.agent.facade.restore_session_runtime_profile", restore)
    status = RuntimePresentationStatus(
        provider="anthropic",
        model="test-model",
        workspace="/workspace",
        profile_configured=True,
        session=SessionPresentationStatus(
            session_id="chat-session",
            title="Chat",
            runtime_profile="chat",
            profile_snapshot=snapshot,
            is_new=False,
        ),
    )
    gateway_session = build_gateway_session(
        SimpleNamespace(runtime_status=lambda: status),
        SimpleNamespace(tree=BottomInputDock().tree),
    )

    thread = gateway_session.list_threads()[0]

    assert thread.runtime_profile == "chat"
    assert gateway_session._runtime_state_provider()["runtime_profile"] == "chat"
    assert restored == [("/workspace", "chat", snapshot)]
    assert gateway_session.resolved_profile("chat-session") is resolved
