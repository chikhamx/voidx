from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from voidx.agent.domain.events import AgentEvent
from voidx.agent.domain.state import SessionRuntimeState
from voidx.agent.domain.thread import AgentThread
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.runtime import AgentRuntime, TurnRequest


@dataclass
class FakeSessions:
    loaded: dict[str, SessionRuntimeState] = field(default_factory=dict)
    saves: list[tuple[str, SessionRuntimeState]] = field(default_factory=list)

    async def load_runtime(self, session_id):
        return self.loaded.get(session_id, SessionRuntimeState()).model_copy(deep=True)

    async def save_runtime(self, session_id, runtime):
        self.saves.append((session_id, runtime.model_copy(deep=True)))


@dataclass
class FakeEvents:
    events: list[AgentEvent] = field(default_factory=list)

    def publish(self, event):
        self.events.append(event)


@dataclass
class FakeEngine:
    session_id: str = ""
    runtime: SessionRuntimeState = field(default_factory=SessionRuntimeState)

    async def run(self, user_text, runtime, *, display_text=None, context=None, persist_user_input=True):
        self.runtime = runtime
        return runtime



@pytest.mark.asyncio
async def test_runtime_advances_turn_phase_around_engine_and_commit():
    sessions = FakeSessions()
    events = FakeEvents()
    engine = FakeEngine()
    observed = {}

    async def run(user_text, runtime, *, display_text=None, context=None, persist_user_input=True):
        observed["phase"] = runtime.turn_phase
        return runtime

    engine.run = run
    runtime = AgentRuntime(type("Resources", (), {"sessions": sessions, "events": events, "turn_engine": engine})())

    result = await runtime.run_turn(
TurnRequest(thread=AgentThread(thread_id="t1"), user_text="hello", context=TurnExecutionContext(thread_id="t1", session_id=""))
    )

    assert observed["phase"].value == "running"
    assert result.runtime.turn_phase.value == "committed"

@pytest.mark.asyncio
async def test_runtime_commits_one_result_and_returns_thread_identity():
    sessions = FakeSessions(loaded={"s1": SessionRuntimeState()})
    events = FakeEvents()
    engine = FakeEngine()
    runtime = AgentRuntime(type("Resources", (), {"sessions": sessions, "events": events, "turn_engine": engine})())

    result = await runtime.run_turn(
        TurnRequest(
            thread=AgentThread(thread_id="t1", session_id="s1"),
            user_text="hello",
            context=TurnExecutionContext(thread_id="t1", session_id="s1"),
        )
    )

    assert result.session_id == "s1"
    assert [session_id for session_id, _ in sessions.saves] == ["s1"]
    assert len(events.events) == 2


@pytest.mark.asyncio
async def test_runtime_loads_state_when_caller_does_not_supply_one():
    stored = SessionRuntimeState(compaction_summary="from store")
    sessions = FakeSessions(loaded={"s1": stored})
    events = FakeEvents()
    engine = FakeEngine()
    runtime = AgentRuntime(type("Resources", (), {"sessions": sessions, "events": events, "turn_engine": engine})())

    await runtime.run_turn(
        TurnRequest(
            thread=AgentThread(thread_id="t1", session_id="s1"),
            user_text="hello",
            context=TurnExecutionContext(thread_id="t1", session_id="s1"),
        )
    )

    assert engine.runtime.compaction_summary == "from store"


@pytest.mark.asyncio
async def test_runtime_prefers_caller_supplied_state_over_store():
    sessions = FakeSessions(loaded={"s1": SessionRuntimeState(compaction_summary="from store")})
    events = FakeEvents()
    engine = FakeEngine()
    runtime = AgentRuntime(type("Resources", (), {"sessions": sessions, "events": events, "turn_engine": engine})())

    await runtime.run_turn(
        TurnRequest(
            thread=AgentThread(thread_id="t1", session_id="s1"),
            user_text="hello",
            context=TurnExecutionContext(thread_id="t1", session_id="s1"),
            runtime=SessionRuntimeState(compaction_summary="from caller"),
        )
    )

    assert engine.runtime.compaction_summary == "from caller"


@pytest.mark.asyncio
async def test_runtime_resolves_lazy_identity_from_engine_session_id():
    sessions = FakeSessions()
    events = FakeEvents()
    engine = FakeEngine()

    async def run(user_text, runtime, *, display_text=None, context=None, persist_user_input=True):
        engine.session_id = "lazy-created"
        return runtime

    engine.run = run
    runtime = AgentRuntime(type("Resources", (), {"sessions": sessions, "events": events, "turn_engine": engine})())

    result = await runtime.run_turn(
        TurnRequest(
            thread=AgentThread(thread_id="t1"),
            user_text="hello",
            context=TurnExecutionContext(thread_id="t1", session_id=""),
        )
    )

    assert result.session_id == "lazy-created"
    assert [session_id for session_id, _ in sessions.saves] == ["lazy-created"]


def test_turn_metadata_from_context_uses_runtime_profile():
    from voidx.agent.domain.loop import LOOP_PROFILE
    from voidx.agent.domain.profile import RuntimeProfile
    from voidx.agent.domain.turn_metadata import turn_metadata_from_context

    coding = turn_metadata_from_context(TurnExecutionContext(thread_id="t1", session_id="s1"))
    assert coding.profile_id == "coding"
    assert coding.protocol == "turn"
    assert coding.category == "coding"

    chat_profile = RuntimeProfile(profile_id="chat", revision=1, name="Chat")
    chat = turn_metadata_from_context(
        TurnExecutionContext(thread_id="chat:s1", session_id="s1", runtime_profile=chat_profile)
    )
    assert chat.profile_id == "chat"
    assert chat.protocol == "turn"
    assert chat.category == "chat"

    loop = turn_metadata_from_context(
        TurnExecutionContext(thread_id="loop:t1:1", session_id="loop-session", runtime_profile=LOOP_PROFILE)
    )
    assert loop.profile_id == "loop"
    assert loop.protocol == "loop"
    assert loop.category == "loop"
