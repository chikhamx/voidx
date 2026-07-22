import pytest

from voidx.agent.application.coding_service import CODING_PROFILE, CodingService
from voidx.agent.domain.thread import LifecycleState
from voidx.agent.runtime.contracts import TurnResult
from voidx.runtime.ui import ThreadExecutionContext


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


def test_coding_profile_identity():
    assert CODING_PROFILE.profile_id == "coding"
    assert CODING_PROFILE.name == "Coding"
    assert CODING_PROFILE.prompt_policy is not None


@pytest.mark.asyncio
async def test_coding_service_delegates_default_coding_turn():
    runtime = FakeRuntime()
    service = CodingService(runtime)

    result = await service.run_turn(user_text="fix it")

    assert result.thread.thread_id == "coding"
    assert len(runtime.requests) == 1
    request = runtime.requests[0]
    assert request.user_text == "fix it"
    assert request.thread.thread_id == "coding"
    assert request.thread.session_id is None
    assert request.profile == CODING_PROFILE
    assert request.runtime is None
    assert request.context is None


@pytest.mark.asyncio
async def test_coding_service_uses_existing_thread_and_session_ids():
    runtime = FakeRuntime()
    service = CodingService(runtime)

    await service.run_turn(
        user_text="continue",
        thread_id="thread-1",
        session_id="session-1",
    )

    request = runtime.requests[0]
    assert request.thread.thread_id == "thread-1"
    assert request.thread.session_id == "session-1"
    assert request.context is None


@pytest.mark.asyncio
async def test_coding_service_falls_back_to_session_id_for_thread_id():
    runtime = FakeRuntime()
    service = CodingService(runtime)

    await service.run_turn(user_text="continue", session_id="session-1")

    request = runtime.requests[0]
    assert request.thread.thread_id == "session-1"
    assert request.thread.session_id == "session-1"


@pytest.mark.asyncio
async def test_coding_service_passes_context_through_unchanged():
    runtime = FakeRuntime()
    service = CodingService(runtime)
    context = ThreadExecutionContext(thread_id="ui-thread", session_id="ui-session")

    await service.run_turn(
        user_text="continue",
        thread_id="thread-1",
        session_id="session-1",
        context=context,
    )

    request = runtime.requests[0]
    assert request.context is context
    assert request.thread.thread_id == "thread-1"
    assert request.thread.session_id == "session-1"
