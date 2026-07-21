from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from voidx.agent.domain.events import AgentEvent
from voidx.agent.domain.state import SessionRuntimeState
from voidx.agent.domain.thread import AgentThread
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

    async def run(self, user_text, runtime, *, display_text=None, context=None):
        self.runtime = runtime
        return runtime



@pytest.mark.asyncio
async def test_runtime_advances_turn_phase_around_engine_and_commit():
    sessions = FakeSessions()
    events = FakeEvents()
    engine = FakeEngine()
    observed = {}

    async def run(user_text, runtime, *, display_text=None, context=None):
        observed["phase"] = runtime.turn_phase
        return runtime

    engine.run = run
    runtime = AgentRuntime(type("Resources", (), {"sessions": sessions, "events": events, "turn_engine": engine})())

    result = await runtime.run_turn(
        TurnRequest(thread=AgentThread(thread_id="t1"), user_text="hello")
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
        TurnRequest(thread=AgentThread(thread_id="t1", session_id="s1"), user_text="hello")
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
        TurnRequest(thread=AgentThread(thread_id="t1", session_id="s1"), user_text="hello")
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
            runtime=SessionRuntimeState(compaction_summary="from caller"),
        )
    )

    assert engine.runtime.compaction_summary == "from caller"


@pytest.mark.asyncio
async def test_runtime_resolves_lazy_identity_from_engine_session_id():
    sessions = FakeSessions()
    events = FakeEvents()
    engine = FakeEngine()

    async def run(user_text, runtime, *, display_text=None, context=None):
        engine.session_id = "lazy-created"
        return runtime

    engine.run = run
    runtime = AgentRuntime(type("Resources", (), {"sessions": sessions, "events": events, "turn_engine": engine})())

    result = await runtime.run_turn(
        TurnRequest(thread=AgentThread(thread_id="t1"), user_text="hello")
    )

    assert result.session_id == "lazy-created"
    assert [session_id for session_id, _ in sessions.saves] == ["lazy-created"]
