"""Session profile resolution at the real SDK composition boundary."""
from contextlib import aclosing

import pytest

from test_headless_resources import setup_agent
from voidx.agent.adapters.persistence.session_repository import create_session
from voidx.agent.application.agent_profile_loader import ProfileLoadError
from voidx.persistence.sqlite import execute_commit


@pytest.mark.asyncio
async def test_missing_profile_without_snapshot_is_rejected_before_turn(setup_agent, tmp_path):
    session = await create_session(workspace=str(tmp_path))
    await execute_commit(
        "UPDATE sessions SET runtime_profile = ?, runtime_profile_snapshot = NULL WHERE id = ?",
        ("unavailable-sdk-profile", session.id),
    )
    async with setup_agent as agent:
        async with aclosing(agent.stream("RESTORE_PROBE", session_id=session.id, workspace=str(tmp_path))) as events:
            with pytest.raises(ProfileLoadError, match="profile_unavailable") as error:
                await anext(events)
            assert error.value.diagnostics[0].code == "profile_unavailable"
            assert "unavailable-sdk-profile" in error.value.diagnostics[0].message


@pytest.mark.asyncio
async def test_pinned_snapshot_wins_over_unavailable_registry_profile(setup_agent, tmp_path, monkeypatch):
    from voidx.agent.application.agent_registry import AgentRegistry

    session = await create_session(workspace=str(tmp_path))
    assert session.profile_snapshot is not None

    def unavailable(self, profile_id):
        raise KeyError(profile_id)

    monkeypatch.setattr(AgentRegistry, "resolve", unavailable)
    async with setup_agent as agent:
        events = [event async for event in agent.stream(
            "RESTORE_PROBE", session_id=session.id, workspace=str(tmp_path),
        )]
    assert events[0].kind == "turn.started"
    assert events[-1].kind == "turn.completed"
    assert {event.session_id for event in events} == {session.id}


@pytest.mark.asyncio
@pytest.mark.parametrize("profile,stored_profile", [
    ("goal", "coding"), ("loop", "coding"),
    ("goal", "loop"), ("loop", "goal"),
    ("coding", "goal"), ("coding", "loop"),
    ("goal", "goal"), ("loop", "loop"), ("coding", "coding"),
])
async def test_sdk_routes_pinned_profile_with_real_intake(
    setup_agent, tmp_path, monkeypatch, profile, stored_profile,
):
    import asyncio
    from langchain_core.messages import AIMessage, ToolMessage
    from test_headless_runtime import FileRoundTripModel
    from voidx.agent.adapters.persistence.session_repository import get_session
    from voidx.agent.application.runtime.runtime import AgentRuntime
    from voidx.llm.adapters import langchain_model_factory
    from voidx.tooling.domain.interaction import InteractionResponse

    class IntakeModel(FileRoundTripModel):
        def _reply(self, messages):
            if profile == "coding" or any(isinstance(m, ToolMessage) for m in messages):
                return AIMessage(content="PROFILE_ROUTE_VERIFIED")
            args = {"goal": "Verify pinned routing"}
            if profile == "goal":
                args.update(acceptance_condition="Routing verified", max_attempts=1)
            return AIMessage(content="", tool_calls=[{
                "id": "profile-intake", "name": f"{profile}_init", "args": args,
            }])

    model = IntakeModel()
    monkeypatch.setattr(langchain_model_factory, "create_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(langchain_model_factory, "create_resolver_model", lambda *a, **kw: model)
    session = await create_session(workspace=str(tmp_path), profile=profile)
    if profile == stored_profile:
        await execute_commit("UPDATE sessions SET runtime_profile_snapshot = NULL WHERE id = ?", (session.id,))
    persisted = await get_session(session.id)
    assert (persisted.profile_snapshot is None) == (profile == stored_profile)
    if profile != stored_profile:
        from voidx.agent.application.agent_profile_snapshot import restore_from_snapshot
        assert restore_from_snapshot(persisted.profile_snapshot).runtime_profile.profile_id == profile
        from voidx.bootstrap import headless

        async def load_with_runtime_drift(session_id):
            loaded = await get_session(session_id)
            loaded.runtime_profile = stored_profile
            return loaded

        monkeypatch.setattr(headless, "get_session", load_with_runtime_drift)
        assert (await headless.get_session(session.id)).runtime_profile == stored_profile

    requests, approvals, events = [], [], []
    original = AgentRuntime.run_turn

    async def record(self, request):
        requests.append(request)
        return await original(self, request)

    monkeypatch.setattr(AgentRuntime, "run_turn", record)
    async with asyncio.timeout(15), setup_agent as agent:
        async for event in agent.stream(
            "goal loop coding: do not infer the profile from this prompt",
            session_id=session.id, workspace=str(tmp_path),
        ):
            events.append(event)
            if event.kind == "interaction.required":
                request = event.payload.request
                approvals.append(request)
                assert request.purpose == profile
                assert (request.session_id, request.thread_id, request.turn_id) == (
                    session.id, session.id, events[0].turn_id,
                )
                assert await agent.submit_interaction(request.interaction_id, InteractionResponse(
                    session_id=request.session_id, thread_id=request.thread_id,
                    turn_id=request.turn_id, value="cancelled",
                ))
    assert events[-1].kind == "turn.completed"
    assert len(requests) == 1
    context = requests[0].context
    assert context.runtime_profile.profile_id == profile
    assert (context.goal_intake_controller is not None) == (profile == "goal")
    assert (context.loop_intake_controller is not None) == (profile == "loop")
    assert len(approvals) == (0 if profile == "coding" else 1)
    assert {event.session_id for event in events} == {session.id}
