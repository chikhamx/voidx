import pytest

from voidx.agent.application.coding_service import CODING_PROFILE, CodingService
from voidx.agent.domain.thread import LifecycleState
from voidx.agent.application.runtime.contracts import TurnResult
from voidx.agent.domain.turn_context import TurnExecutionContext


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
    assert request.context.runtime_profile.profile_id == "coding"
    assert request.context.workflow_context is not None
    from voidx.agent.domain.tool_policy import ProfileToolPolicy

    assert isinstance(request.context.tool_policy, ProfileToolPolicy)
    assert request.context.tool_policy.snapshot_hash
    assert request.context.tool_policy.phase == "turn"
    assert request.runtime is None
    assert request.context is not None


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
    assert request.context is not None


@pytest.mark.asyncio
async def test_coding_service_falls_back_to_session_id_for_thread_id():
    runtime = FakeRuntime()
    service = CodingService(runtime)

    await service.run_turn(user_text="continue", session_id="session-1")

    request = runtime.requests[0]
    assert request.thread.thread_id == "session-1"
    assert request.thread.session_id == "session-1"


@pytest.mark.asyncio
async def test_coding_service_populates_default_context_workspace():
    runtime = FakeRuntime()
    service = CodingService(runtime)

    await service.run_turn(
        user_text="continue",
        session_id="session-1",
        workspace="/tmp/workspace",
    )

    request = runtime.requests[0]
    assert request.context.thread_id == "session-1"
    assert request.context.session_id == "session-1"
    assert request.context.workspace == "/tmp/workspace"
    assert request.context.runtime_profile.profile_id == "coding"


@pytest.mark.asyncio
async def test_coding_service_accepts_queued_context_with_workspace():
    runtime = FakeRuntime()
    service = CodingService(runtime)
    context = TurnExecutionContext(
        thread_id="session-1",
        session_id="session-1",
        runtime_profile=CODING_PROFILE,
        workspace="/tmp/workspace",
    )

    await service.run_turn(
        user_text="continue",
        session_id="session-1",
        context=context,
    )

    request = runtime.requests[0]
    assert request.thread.thread_id == "session-1"
    assert request.thread.session_id == "session-1"
    assert request.context.workspace == "/tmp/workspace"
    assert request.context.runtime_profile == CODING_PROFILE


@pytest.mark.asyncio
async def test_coding_service_preserves_context_when_identity_matches():
    runtime = FakeRuntime()
    service = CodingService(runtime)
    tool_policy = object()
    context = TurnExecutionContext(
        thread_id="session-1",
        session_id="session-1",
        runtime_profile=CODING_PROFILE,
        workspace="/tmp/workspace",
        tool_policy=tool_policy,
    )

    await service.run_turn(
        user_text="continue",
        session_id="session-1",
        context=context,
    )

    request = runtime.requests[0]
    assert request.context is context
    assert request.context.workspace == "/tmp/workspace"
    assert request.context.tool_policy is tool_policy

@pytest.mark.asyncio
async def test_coding_service_rejects_context_identity_mismatch():
    runtime = FakeRuntime()
    service = CodingService(runtime)
    context = TurnExecutionContext(thread_id="ui-thread", session_id="ui-session", runtime_profile=CODING_PROFILE)

    with pytest.raises(ValueError, match="context does not match"):
        await service.run_turn(
            user_text="continue",
            thread_id="thread-1",
            session_id="session-1",
            context=context,
        )
