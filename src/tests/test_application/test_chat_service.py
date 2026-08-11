from pathlib import Path

import pytest

from tests.test_application.input_ports import service_ports
from voidx.agent.adapters.input_router import LangGraphAutonomousInputRouter
from voidx.agent.ports.presentation import NullAgentEventPublisher

from voidx.agent.application.chat_service import ChatService
from voidx.agent.domain.state import SessionRuntimeState
from voidx.agent.domain.thread import AgentThread, LifecycleState
from voidx.agent.application.runtime.contracts import TurnResult


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
async def test_chat_service_creates_isolated_session_and_delegates():
    runtime = FakeRuntime()
    calls = []

    async def fake_create_session(**kwargs):
        calls.append(kwargs)
        return type("CreatedSession", (), {"id": "chat-session"})()

    service = ChatService(runtime, session_creator=fake_create_session)

    result = await service.run_turn(user_text="hello")

    assert result.session_id == "chat-session"
    assert calls == [{"workspace": "", "directory": "", "profile": "chat"}]
    assert result.thread.thread_id == "chat:chat-session"
    assert len(runtime.requests) == 1
    assert runtime.requests[0].context.runtime_profile.profile_id == "chat"
    assert runtime.requests[0].context.tool_policy.scope.workspace is None
    assert runtime.requests[0].context.session_id == "chat-session"
    assert runtime.requests[0].context.thread_id == "chat:chat-session"


@pytest.mark.asyncio
async def test_chat_service_preserves_existing_chat_thread():
    runtime = FakeRuntime()
    service = ChatService(runtime, session_creator=None)  # type: ignore[arg-type]
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
    from voidx.agent.adapters.persistence.session_repository import SessionInfo

    class FakeChatService:
        def __init__(self):
            self.calls = []

        async def run_chat_turn(self, **kwargs):
            self.calls.append(kwargs)

    chat = FakeChatService()
    router = LangGraphAutonomousInputRouter(SimpleNamespace(session_id="chat-session"), None, NullAgentEventPublisher(), SimpleNamespace(), chat_service=chat, coding_service=None, loop_service=None, goal_service=None)

    async def fake_get_session(session_id):
        assert session_id == "chat-session"
        return SessionInfo(id=session_id, runtime_profile="chat")

    monkeypatch.setattr("voidx.agent.adapters.persistence.session_repository.get_session", fake_get_session)

    routed = await router.route_chat_turn("hello", thread_id="")

    assert routed is True
    assert len(chat.calls) == 1
    assert chat.calls[0]["thread"].thread_id == "chat:chat-session"


@pytest.mark.asyncio
async def test_route_chat_turn_does_not_route_coding_session(monkeypatch):
    from types import SimpleNamespace

    from voidx.agent.application.agent_service import AgentService
    from voidx.agent.adapters.persistence.session_repository import SessionInfo

    class FakeChatService:
        async def run_chat_turn(self, **kwargs):
            raise AssertionError("coding session must not route to chat")

    router = LangGraphAutonomousInputRouter(SimpleNamespace(session_id="coding-session"), None, NullAgentEventPublisher(), SimpleNamespace(), chat_service=FakeChatService(), coding_service=None, loop_service=None, goal_service=None)

    async def fake_get_session(session_id):
        return SessionInfo(id=session_id, runtime_profile="coding")

    monkeypatch.setattr("voidx.agent.adapters.persistence.session_repository.get_session", fake_get_session)

    routed = await router.route_chat_turn("hello", thread_id="")

    assert routed is False


@pytest.mark.asyncio
async def test_route_chat_turn_trusts_context_profile_without_db(monkeypatch):
    """Gateway turns carry a full TurnExecutionContext; routing must trust its
    profile instead of re-querying the session repository."""
    from types import SimpleNamespace

    from voidx.agent.application.chat_service import CHAT_PROFILE
    from voidx.agent.domain.turn_context import TurnExecutionContext

    class FakeChatService:
        def __init__(self):
            self.calls = []

        async def run_chat_turn(self, **kwargs):
            self.calls.append(kwargs)

    chat = FakeChatService()
    router = LangGraphAutonomousInputRouter(SimpleNamespace(session_id="host-session"), None, NullAgentEventPublisher(), SimpleNamespace(), chat_service=chat, coding_service=None, loop_service=None, goal_service=None)

    async def fail_get_session(session_id):
        raise AssertionError("routing must not query the repository when context is provided")

    monkeypatch.setattr("voidx.agent.adapters.persistence.session_repository.get_session", fail_get_session)

    context = TurnExecutionContext(
        thread_id="chat-thread",
        session_id="chat-thread",
        runtime_profile=CHAT_PROFILE,
        workspace="/tmp/ws",
    )

    routed = await router.route_chat_turn("hello", thread_id="", context=context)

    assert routed is True
    assert len(chat.calls) == 1
    assert chat.calls[0]["thread"].thread_id == "chat:chat-thread"
    assert chat.calls[0]["thread"].session_id == "chat-thread"
    assert chat.calls[0]["workspace"] == "/tmp/ws"


@pytest.mark.asyncio
async def test_route_chat_turn_context_coding_returns_false_without_db(monkeypatch):
    from types import SimpleNamespace

    from voidx.agent.domain.profile import CODING_PROFILE
    from voidx.agent.domain.turn_context import TurnExecutionContext

    class FakeChatService:
        async def run_chat_turn(self, **kwargs):
            raise AssertionError("coding context must not route to chat")

    router = LangGraphAutonomousInputRouter(SimpleNamespace(session_id="host-session"), None, NullAgentEventPublisher(), SimpleNamespace(), chat_service=FakeChatService(), coding_service=None, loop_service=None, goal_service=None)

    async def fail_get_session(session_id):
        raise AssertionError("routing must not query the repository when context is provided")

    monkeypatch.setattr("voidx.agent.adapters.persistence.session_repository.get_session", fail_get_session)

    context = TurnExecutionContext(
        thread_id="coding-thread",
        session_id="coding-thread",
        runtime_profile=CODING_PROFILE,
    )

    routed = await router.route_chat_turn("hello", thread_id="", context=context)

    assert routed is False
