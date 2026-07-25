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
