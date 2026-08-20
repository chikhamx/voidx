from __future__ import annotations

from dataclasses import dataclass, replace

import pytest
from voidx.presentation.gateway.session import GatewaySession
from voidx.presentation.output.dock import BottomInputDock
from voidx.presentation.protocol.v2.envelope import JsonRpcRequest, JsonRpcResult




@dataclass(frozen=True)
class SessionInfo:
    id: str
    title: str = "New session"
    workspace: str = "."
    directory: str = ""
    model_provider: str = "anthropic"
    model_name: str = ""
    created_at: str = ""
    updated_at: str = ""
    message_count: int = 0
    runtime_profile: str = "coding"
    profile_snapshot: object | None = None


class FakeSessionRepository:
    def __init__(self) -> None:
        self.sessions: dict[str, SessionInfo] = {}
        self.calls: list[tuple] = []
        self._next_id = 1

    async def create_session(
        self,
        *,
        workspace: str,
        title: str = "New session",
        directory: str = "",
        provider: str = "anthropic",
        model: str = "",
        profile: str = "coding",
    ) -> SessionInfo:
        self.calls.append(("create", workspace, title, directory, provider, model, profile))
        if profile not in {"chat", "coding", "loop", "goal"}:
            raise ValueError(f"unknown runtime profile: {profile}")
        info = SessionInfo(
            id=f"session-{self._next_id}",
            title=title,
            workspace=workspace,
            directory=directory,
            model_provider=provider,
            model_name=model,
            runtime_profile=profile,
        )
        self._next_id += 1
        self.sessions[info.id] = info
        return info

    async def stage_provisional_session(self, **kwargs) -> SessionInfo:
        self.calls.append(("stage", kwargs))
        info = SessionInfo(
            id=kwargs["session_id"],
            title=kwargs.get("title", "New session"),
            workspace=kwargs.get("workspace", "."),
            directory=kwargs.get("directory", ""),
            model_provider=kwargs.get("provider", "anthropic"),
            model_name=kwargs.get("model", ""),
            runtime_profile=kwargs.get("profile", "coding"),
        )
        self.sessions[info.id] = info
        return info

    async def promote_provisional_session(self, session_id: str) -> int:
        self.calls.append(("promote", session_id))
        return 1

    async def rollback_provisional_session(self, session_id: str) -> int:
        self.calls.append(("rollback", session_id))
        self.sessions.pop(session_id, None)
        return 1

    async def initialize_provisional_owner(self, owner_id: str) -> list[str]:
        self.calls.append(("initialize_owner", owner_id))
        return []

    async def close_provisional_owner(self, owner_id: str) -> int:
        self.calls.append(("close_owner", owner_id))
        return 0

    async def list_sessions(self, *, limit: int = 50) -> list[SessionInfo]:
        self.calls.append(("list", limit))
        return list(self.sessions.values())[:limit]

    async def fork_session(self, session_id: str, *, title: str | None = None) -> SessionInfo | None:
        self.calls.append(("fork", session_id, title))
        source = self.sessions.get(session_id)
        if source is None:
            return None
        forked = replace(
            source,
            id=f"session-{self._next_id}",
            title=title or f"{source.title} (fork)",
        )
        self._next_id += 1
        self.sessions[forked.id] = forked
        return forked

    async def delete_session(self, session_id: str) -> None:
        self.calls.append(("delete", session_id))
        self.sessions.pop(session_id, None)

    async def update_title(self, session_id: str, title: str) -> None:
        self.calls.append(("rename", session_id, title))
        self.sessions[session_id] = replace(self.sessions[session_id], title=title)


async def dispatch(session: GatewaySession, request_id: int, method: str, params: dict) -> dict:
    response = await session.dispatch_request(
        JsonRpcRequest(id=request_id, method=method, params=params),
    )
    assert isinstance(response, JsonRpcResult)
    return response.result


@pytest.mark.asyncio
async def test_sync_persisted_thread_restores_pinned_profile_snapshot(monkeypatch) -> None:
    snapshot = object()
    resolved = object()
    repository = FakeSessionRepository()
    repository.sessions["persisted-chat"] = SessionInfo(
        id="persisted-chat",
        workspace="/workspace",
        runtime_profile="chat",
        profile_snapshot=snapshot,
    )
    restored: list[tuple[str, str, object]] = []

    def restore(workspace: str, profile_id: str, value: object) -> object:
        restored.append((workspace, profile_id, value))
        return resolved

    monkeypatch.setattr(
        "voidx.agent.facade.restore_session_runtime_profile",
        restore,
    )
    session = GatewaySession(
        lambda: BottomInputDock().tree,
        thread_id="persisted-chat",
        runtime_profile="chat",
        workspace="/workspace",
        session_repository=repository,
    )

    await session.sync_persisted_threads()

    assert restored == [("/workspace", "chat", snapshot)]
    assert session.resolved_profile("persisted-chat") is resolved


@pytest.mark.asyncio
async def test_temporary_session_crud_stays_in_memory() -> None:
    repository = FakeSessionRepository()
    session = GatewaySession(
        lambda: BottomInputDock().tree,
        workspace="/workspace",
        session_repository=repository,
    )

    created = await dispatch(
        session,
        1,
        "session.create",
        {"title": "Original", "directory": "project", "profile": "goal"},
    )
    session_id = created["thread_id"]
    assert created == {
        "thread_id": session_id,
        "active_thread_id": session_id,
        "title": "Original",
        "directory": "project",
        "workspace": "project",
        "status": "idle",
        "runtime_profile": "goal",
        "temporary": True,
    }

    listed = await dispatch(session, 2, "session.list", {})
    assert listed["threads"] == [
        {
            "thread_id": session_id,
            "title": "Original",
            "workspace": "project",
            "directory": "project",
            "model_provider": "",
            "model_name": "",
            "status": "idle",
            "created_at": "",
            "updated_at": "",
            "message_count": 0,
            "runtime_profile": "goal",
            "temporary": True,
        }
    ]

    assert await dispatch(
        session,
        3,
        "session.rename",
        {"thread_id": session_id, "title": "Renamed"},
    ) == {"ok": True}
    assert session.list_threads()[0].title == "Renamed"

    assert await dispatch(
        session,
        4,
        "session.delete",
        {"thread_id": session_id},
    ) == {"ok": True}
    assert session.list_threads() == []
    assert repository.calls == [("list", 200)]




@pytest.mark.asyncio
async def test_gateway_provisional_owner_lifecycle_uses_repository_port() -> None:
    repository = FakeSessionRepository()
    session = GatewaySession(
        lambda: BottomInputDock().tree,
        workspace="/workspace",
        session_repository=repository,
    )

    await session.initialize_provisional_lifecycle()
    await session.close_provisional_lifecycle()

    assert repository.calls == [
        ("initialize_owner", session.owner_id),
        ("close_owner", session.owner_id),
    ]
@pytest.mark.asyncio
async def test_first_successful_turn_promotes_temporary_session() -> None:
    from voidx.presentation.output.events.schema import TurnCompleted

    repository = FakeSessionRepository()
    received = []

    async def handle(command) -> None:
        received.append(command)

    session = GatewaySession(
        lambda: BottomInputDock().tree,
        workspace="/workspace",
        session_repository=repository,
        command_handler=handle,
    )
    created = await dispatch(session, 10, "session.create", {"profile": "chat"})
    thread_id = created["thread_id"]

    assert await dispatch(
        session, 11, "session.submit", {"thread_id": thread_id, "text": "hello"}
    ) == {"ok": True}
    assert repository.calls[0][0] == "stage"
    staged = repository.calls[0][1]
    snapshot = staged.pop("profile_snapshot")
    assert staged == {
        "owner_id": session.owner_id,
        "session_id": thread_id,
        "workspace": "/workspace",
        "directory": "",
        "title": "New session",
        "profile": "chat",
    }
    assert snapshot.profile_id == "chat"
    assert snapshot.revision == 1
    assert session.list_threads()[0].temporary is True

    await session.broadcast_event(TurnCompleted(), thread_id=thread_id)

    assert repository.calls[-1] == ("promote", thread_id)
    assert session.list_threads()[0].temporary is False
    assert session.list_threads()[0].status == "idle"


@pytest.mark.asyncio
async def test_chat_runtime_events_stay_attached_to_the_session_thread() -> None:
    from tests.test_presentation.gateway.helpers import FakeClient
    from voidx.presentation.output.events.schema import TurnCompleted, TurnStarted

    repository = FakeSessionRepository()
    session = GatewaySession(
        lambda: BottomInputDock().tree,
        workspace="/workspace",
        session_repository=repository,
        command_handler=lambda command: None,
    )
    await session.connect(FakeClient())
    created = await dispatch(session, 40, "session.create", {"profile": "chat"})
    thread_id = created["thread_id"]

    await dispatch(session, 41, "session.submit", {"thread_id": thread_id, "text": "hello"})
    await session.broadcast_event(
        TurnStarted(text="hello", thread_id=f"chat:{thread_id}"),
    )
    await session.broadcast_event(TurnCompleted(thread_id=f"chat:{thread_id}"))

    assert session.active_thread_id == thread_id
    assert [(thread.thread_id, thread.runtime_profile) for thread in session.list_threads()] == [
        (thread_id, "chat"),
    ]
    assert session.list_threads()[0].temporary is False
    assert repository.calls[-1] == ("promote", thread_id)




@pytest.mark.asyncio
async def test_control_slash_does_not_stage_or_activate_temporary_session() -> None:
    repository = FakeSessionRepository()
    received = []
    session = GatewaySession(
        lambda: BottomInputDock().tree,
        workspace="/workspace",
        session_repository=repository,
        command_handler=lambda command: received.append(command),
    )
    created = await dispatch(session, 12, "session.create", {"profile": "coding"})
    thread_id = created["thread_id"]

    assert await dispatch(
        session, 13, "session.submit", {"thread_id": thread_id, "text": "/help"}
    ) == {"ok": True}

    assert repository.calls == []
    assert len(received) == 1
    assert received[0].text == "/help"
    assert session.list_threads()[0].temporary is True
    assert session.list_threads()[0].status == "idle"
@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_kind", ["failed", "cancelled"])
async def test_unsuccessful_first_turn_rolls_back_and_keeps_temporary_session(
    terminal_kind: str,
) -> None:
    from voidx.presentation.output.events.schema import TurnCancelled, TurnFailed

    repository = FakeSessionRepository()
    session = GatewaySession(
        lambda: BottomInputDock().tree,
        workspace="/workspace",
        session_repository=repository,
        command_handler=lambda command: None,
    )
    created = await dispatch(session, 20, "session.create", {"profile": "goal"})
    thread_id = created["thread_id"]
    await dispatch(session, 21, "session.submit", {"thread_id": thread_id, "text": "try"})

    event = TurnFailed(message="boom") if terminal_kind == "failed" else TurnCancelled()
    await session.broadcast_event(event, thread_id=thread_id)

    assert repository.calls[-1] == ("rollback", thread_id)
    thread = session.list_threads()[0]
    assert thread.thread_id == thread_id
    assert thread.temporary is True
    assert thread.status == "idle"

    await dispatch(session, 22, "session.submit", {"thread_id": thread_id, "text": "retry"})
    assert [call[0] for call in repository.calls].count("stage") == 2


@pytest.mark.asyncio
async def test_temporary_lifecycle_broadcasts_authoritative_snapshots() -> None:
    import json

    from tests.test_presentation.gateway.helpers import FakeClient
    from voidx.presentation.output.events.schema import TurnCompleted, TurnFailed

    repository = FakeSessionRepository()
    session = GatewaySession(
        lambda: BottomInputDock().tree,
        workspace="/workspace",
        session_repository=repository,
        command_handler=lambda command: None,
    )
    client = FakeClient()
    await session.connect(client)
    created = await dispatch(session, 30, "session.create", {"profile": "coding"})
    thread_id = created["thread_id"]
    client.messages.clear()

    await dispatch(session, 31, "session.submit", {"thread_id": thread_id, "text": "work"})
    submit_snapshots = [
        json.loads(message)["params"]
        for message in client.messages
        if json.loads(message).get("method") == "workspace.snapshot"
    ]
    assert any(
        thread["thread_id"] == thread_id
        and thread["temporary"] is True
        and thread["status"] == "running"
        for snapshot in submit_snapshots
        for thread in snapshot["threads"]
    )

    client.messages.clear()
    await session.broadcast_event(TurnCompleted(), thread_id=thread_id)
    promoted = json.loads(client.messages[-1])
    assert promoted["method"] == "workspace.snapshot"
    promoted_thread = next(
        thread for thread in promoted["params"]["threads"] if thread["thread_id"] == thread_id
    )
    assert promoted_thread["temporary"] is False
    assert promoted_thread["status"] == "idle"

    created = await dispatch(session, 32, "session.create", {"profile": "coding"})
    failed_id = created["thread_id"]
    await dispatch(session, 33, "session.submit", {"thread_id": failed_id, "text": "fail"})
    client.messages.clear()
    await session.broadcast_event(TurnFailed(message="boom"), thread_id=failed_id)
    rolled_back = json.loads(client.messages[-1])
    assert rolled_back["method"] == "workspace.snapshot"
    failed_thread = next(
        thread for thread in rolled_back["params"]["threads"] if thread["thread_id"] == failed_id
    )
    assert failed_thread["temporary"] is True
    assert failed_thread["status"] == "idle"


@pytest.mark.asyncio
async def test_concurrent_first_submit_stages_temporary_thread_once() -> None:
    import asyncio

    repository = FakeSessionRepository()
    stage_started = asyncio.Event()
    release_stage = asyncio.Event()
    original_stage = repository.stage_provisional_session

    async def slow_stage(**kwargs):
        stage_started.set()
        await release_stage.wait()
        return await original_stage(**kwargs)

    repository.stage_provisional_session = slow_stage
    received = []
    session = GatewaySession(
        lambda: BottomInputDock().tree,
        workspace="/workspace",
        session_repository=repository,
        command_handler=lambda command: received.append(command),
    )
    created = await dispatch(session, 40, "session.create", {"profile": "coding"})
    thread_id = created["thread_id"]

    first = asyncio.create_task(
        dispatch(session, 41, "session.submit", {"thread_id": thread_id, "text": "first"})
    )
    await stage_started.wait()
    second = asyncio.create_task(
        dispatch(session, 42, "session.submit", {"thread_id": thread_id, "text": "second"})
    )
    release_stage.set()
    assert await first == {"ok": True}
    assert await second == {"ok": True}

    assert [call[0] for call in repository.calls].count("stage") == 1
    assert len(received) == 2
    assert received[0].text == "first"
    assert received[1]["kind"] == "guide"
