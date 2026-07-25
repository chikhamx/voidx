from __future__ import annotations

from dataclasses import dataclass, field

import asyncio

import pytest

from voidx.agent.application.compaction_service import CompactionService
from voidx.agent.application.session_service import SessionService
from voidx.agent.application.tool_service import ToolService
from types import SimpleNamespace

from voidx.agent.domain.thread import AgentThread
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.runtime import AgentRuntime, TurnRequest
from voidx.agent.domain.compaction import CompactionResult
from voidx.agent.domain.events import AgentEventKind
from voidx.agent.domain.state import SessionRuntimeState
from voidx.agent.ports.tools import ToolExecutionResult


@dataclass
class MemorySessionStore:
    runtimes: dict[str, SessionRuntimeState] = field(default_factory=dict)
    cleared: list[str] = field(default_factory=list)

    async def load_runtime(self, session_id: str) -> SessionRuntimeState:
        return self.runtimes.get(session_id, SessionRuntimeState()).model_copy(deep=True)

    async def save_runtime(self, session_id: str, runtime: SessionRuntimeState) -> None:
        self.runtimes[session_id] = runtime.model_copy(deep=True)

    async def clear_runtime(self, session_id: str) -> None:
        self.cleared.append(session_id)
        self.runtimes.pop(session_id, None)


@pytest.mark.asyncio
async def test_session_service_restores_persists_and_clears_runtime():
    stored = SessionRuntimeState(compaction_summary="saved", session_time="2026-07-19 CST")
    store = MemorySessionStore(runtimes={"s1": stored})
    service = SessionService(store)

    restored = await service.restore_runtime("s1")
    restored.compaction_summary = "updated"
    await service.persist_runtime("s1", restored)
    await service.clear_runtime("s1")

    assert restored.session_time == "2026-07-19 CST"
    assert store.cleared == ["s1"]
    assert "s1" not in store.runtimes


@dataclass
class FakeCompactionEngine:
    result: CompactionResult | None
    calls: list[tuple[list, list | None, bool, bool, bool]] = field(default_factory=list)

    async def compact(self, messages, session_messages=None, *, force=False, ask=True, preflight=False):
        self.calls.append((messages, session_messages, force, ask, preflight))
        return self.result


@pytest.mark.asyncio
async def test_compaction_service_applies_result_and_emits_semantic_event():
    result = CompactionResult(
        summary="summary",
        removed_messages=["old"],
        live_messages=["new"],
        tail_id="tail-1",
    )
    engine = FakeCompactionEngine(result)
    events = []
    service = CompactionService(engine, events.append)
    messages = ["old", "new"]

    removed, tail_id = await service.compact_live_messages(messages, force=True, ask=False)

    assert messages == ["new"]
    assert removed == ["old"]
    assert tail_id == "tail-1"
    assert events[0].kind is AgentEventKind.COMPACTION_COMPLETED


@dataclass
class FakePermission:
    allowed: bool
    calls: list[tuple[str, dict]] = field(default_factory=list)

    async def authorize(self, tool_name: str, arguments: dict) -> bool:
        self.calls.append((tool_name, arguments))
        return self.allowed


@dataclass
class FakeTools:
    result: ToolExecutionResult
    calls: list[tuple[str, dict]] = field(default_factory=list)

    async def execute(self, tool_name: str, arguments: dict) -> ToolExecutionResult:
        self.calls.append((tool_name, arguments))
        return self.result


@pytest.mark.asyncio
async def test_tool_service_checks_permission_before_execution():
    permission = FakePermission(allowed=False)
    tools = FakeTools(ToolExecutionResult(output="should not run"))

    result = await ToolService(permission, tools).execute("write", {"path": "x"})

    assert result.denied is True
    assert tools.calls == []


@dataclass
class FakeTurnEngine:
    fail: bool = False
    calls: list[tuple[str, SessionRuntimeState, str | None]] = field(default_factory=list)
    session_id: str = ""

    async def run(
        self,
        user_text: str,
        runtime: SessionRuntimeState,
        *,
        display_text: str | None = None,
        context=None,
    ) -> SessionRuntimeState:
        self.calls.append((user_text, runtime, display_text))
        if self.fail:
            raise RuntimeError("engine failed")
        return runtime.model_copy(update={"compaction_summary": "completed"})


@dataclass
class MemoryEvents:
    events: list = field(default_factory=list)

    def publish(self, event) -> None:
        self.events.append(event)


def _runtime(engine, sessions, events) -> AgentRuntime:
    return AgentRuntime(
        SimpleNamespace(turn_engine=engine, sessions=sessions, events=events)
    )


def _request(session_id: str, text: str, **kwargs) -> TurnRequest:
    return TurnRequest(
        thread=AgentThread(thread_id=session_id or "coding", session_id=session_id or None),
        user_text=text,
        context=TurnExecutionContext(
            thread_id=session_id or "coding",
            session_id=session_id or "",
        ),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_runtime_orders_events_and_persists_success():
    sessions = MemorySessionStore()
    events = MemoryEvents()
    runtime = _runtime(FakeTurnEngine(), sessions, events)

    result = await runtime.run_turn(_request("s1", "hello", display_text="focus"))

    assert result.runtime.compaction_summary == "completed"
    assert [event.kind for event in events.events] == [
        AgentEventKind.TURN_STARTED,
        AgentEventKind.TURN_COMPLETED,
    ]
    assert sessions.runtimes["s1"].compaction_summary == "completed"


@pytest.mark.asyncio
async def test_runtime_persists_failure_and_emits_failed_event():
    sessions = MemorySessionStore()
    events = MemoryEvents()
    runtime = _runtime(FakeTurnEngine(fail=True), sessions, events)

    with pytest.raises(RuntimeError, match="engine failed"):
        await runtime.run_turn(_request("s1", "hello"))

    assert "s1" in sessions.runtimes
    assert [event.kind for event in events.events] == [
        AgentEventKind.TURN_STARTED,
        AgentEventKind.TURN_FAILED,
    ]


@pytest.mark.asyncio
async def test_runtime_cancel_persists_and_propagates_cancellation():
    class CancelledEngine:
        session_id = ""

        async def run(self, user_text, runtime, *, display_text=None, context=None):
            raise asyncio.CancelledError

    sessions = MemorySessionStore()
    events = MemoryEvents()
    runtime = _runtime(CancelledEngine(), sessions, events)

    with pytest.raises(asyncio.CancelledError):
        await runtime.run_turn(_request("s1", "hello"))

    assert "s1" in sessions.runtimes
    assert events.events[-1].metadata["cancelled"] is True
