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
async def test_session_crud_uses_injected_repository_port() -> None:
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
    }

    listed = await dispatch(session, 2, "session.list", {})
    assert [thread["thread_id"] for thread in listed["threads"]] == [session_id]

    forked = await dispatch(
        session,
        3,
        "session.fork",
        {"thread_id": session_id, "title": "Forked"},
    )
    assert forked["title"] == "Forked"

    assert await dispatch(
        session,
        4,
        "session.rename",
        {"thread_id": session_id, "title": "Renamed"},
    ) == {"ok": True}
    assert await dispatch(
        session,
        5,
        "session.delete",
        {"thread_id": session_id},
    ) == {"ok": True}

    assert repository.calls == [
        ("create", "project", "Original", "project", "anthropic", "", "goal"),
        ("list", 200),
        ("fork", session_id, "Forked"),
        ("rename", session_id, "Renamed"),
        ("delete", session_id),
    ]
