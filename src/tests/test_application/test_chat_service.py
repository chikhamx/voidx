from pathlib import Path

import pytest

from voidx.agent.application.chat_service import ChatService
from voidx.agent.domain.state import SessionRuntimeState
from voidx.agent.domain.thread import AgentThread, LifecycleState
from voidx.agent.runtime.contracts import TurnResult


class FakeRuntime:
    def __init__(self):
        self.requests = []

    async def run_turn(self, request):
        self.requests.append(request)
        return TurnResult(
            thread=request.thread.model_copy(
                update={"session_id": request.thread.session_id, "lifecycle": LifecycleState.COMPLETED}
            ),
            lifecycle=LifecycleState.COMPLETED,
            runtime=request.runtime,
        )


@pytest.mark.asyncio
async def test_chat_service_creates_isolated_session_and_delegates(monkeypatch):
    runtime = FakeRuntime()

    async def fake_create_session(**kwargs):
        from voidx.memory.service import SessionInfo
        return SessionInfo(id="chat-session", workspace="", runtime_profile="chat")

    monkeypatch.setattr("voidx.agent.application.chat_service.create_session", fake_create_session)
    service = ChatService(runtime)

    result = await service.run_turn(user_text="hello")

    assert result.session_id == "chat-session"
    assert result.thread.thread_id == "chat:chat-session"
    assert len(runtime.requests) == 1
    assert runtime.requests[0].context.runtime_profile.profile_id == "chat"
    assert runtime.requests[0].context.tool_policy.scope.workspace is None
    assert runtime.requests[0].context.session_id == "chat-session"
    assert runtime.requests[0].context.thread_id == "chat:chat-session"


@pytest.mark.asyncio
async def test_chat_service_preserves_existing_chat_thread():
    runtime = FakeRuntime()
    service = ChatService(runtime)
    thread = AgentThread(thread_id="chat:existing", session_id="existing")

    result = await service.run_turn(
        thread=thread,
        user_text="continue",
        runtime_state=SessionRuntimeState(),
        workspace=Path("/tmp/project"),
    )

    assert result.session_id == "existing"
    assert runtime.requests[0].thread.thread_id == "chat:existing"
    assert runtime.requests[0].context.tool_policy.scope.workspace == Path("/tmp/project").resolve()
    assert runtime.requests[0].context.session_id == "existing"


@pytest.mark.asyncio
async def test_route_chat_turn_routes_resumed_chat_session_when_thread_id_empty(monkeypatch):
    """/chat resumes a chat-profile session; a turn on the host session must
    still be routed to ChatService even when the caller passes no thread_id."""
    from types import SimpleNamespace

    from voidx.agent.application.agent_service import AgentService
    from voidx.memory.service import SessionInfo

    class FakeChatService:
        def __init__(self):
            self.calls = []

        async def run_turn(self, **kwargs):
            self.calls.append(kwargs)

    chat = FakeChatService()
    service = AgentService(SimpleNamespace(session_id="chat-session"), runtime=None, chat_service=chat)

    async def fake_get_session(session_id):
        assert session_id == "chat-session"
        return SessionInfo(id=session_id, runtime_profile="chat")

    monkeypatch.setattr("voidx.memory.service.get_session", fake_get_session)

    routed = await service._route_chat_turn("hello", thread_id="")

    assert routed is True
    assert len(chat.calls) == 1
    assert chat.calls[0]["thread"].thread_id == "chat:chat-session"


@pytest.mark.asyncio
async def test_route_chat_turn_does_not_route_coding_session(monkeypatch):
    from types import SimpleNamespace

    from voidx.agent.application.agent_service import AgentService
    from voidx.memory.service import SessionInfo

    class FakeChatService:
        async def run_turn(self, **kwargs):
            raise AssertionError("coding session must not route to chat")

    service = AgentService(
        SimpleNamespace(session_id="coding-session"), runtime=None, chat_service=FakeChatService()
    )

    async def fake_get_session(session_id):
        return SessionInfo(id=session_id, runtime_profile="coding")

    monkeypatch.setattr("voidx.memory.service.get_session", fake_get_session)

    routed = await service._route_chat_turn("hello", thread_id="")

    assert routed is False
