from __future__ import annotations

from dataclasses import dataclass, field

import asyncio

import pytest

from tests.test_application.input_ports import service_ports

from voidx.agent.application.coding_service import CodingService
from voidx.agent.application.compaction_service import CompactionService
from voidx.agent.application.session_service import SessionService
from voidx.agent.application.tool_service import ToolService
from types import SimpleNamespace

from voidx.agent.domain.thread import AgentThread
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.application.runtime import AgentRuntime, TurnRequest
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
    calls: list[tuple[str, SessionRuntimeState, str | None, bool]] = field(default_factory=list)
    session_id: str = ""

    async def run(
        self,
        user_text: str,
        runtime: SessionRuntimeState,
        *,
        display_text: str | None = None,
        context=None,
        persist_user_input: bool = True,
    ) -> SessionRuntimeState:
        self.calls.append((user_text, runtime, display_text, persist_user_input))
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

        async def run(self, user_text, runtime, *, display_text=None, context=None, persist_user_input=True):
            raise asyncio.CancelledError

    sessions = MemorySessionStore()
    events = MemoryEvents()
    runtime = _runtime(CancelledEngine(), sessions, events)

    with pytest.raises(asyncio.CancelledError):
        await runtime.run_turn(_request("s1", "hello"))

    assert "s1" in sessions.runtimes
    assert events.events[-1].metadata["cancelled"] is True


@dataclass
class FakeCodingService:
    calls: list[dict] = field(default_factory=list)

    async def run_coding_turn(self, **kwargs):
        self.calls.append(kwargs)


@dataclass
class FakeExecutionHost:
    session_id: str = "session-1"
    workspace: str = "/tmp/workspace"
    bound_coding_turn_runner: object | None = None

    def bind_coding_turn_runner(self, runner) -> None:
        self.bound_coding_turn_runner = runner




def _agent_service_with_coding(execution, coding_service):
    from voidx.agent.application.agent_service import AgentService
    from voidx.agent.infrastructure.input_router import LangGraphAutonomousInputRouter
    from voidx.agent.ports.presentation import NullAgentEventPublisher
    from tests.test_application.input_ports import FakeInputPorts

    ports = FakeInputPorts(session_id=execution.session_id, workspace=execution.workspace)
    router = LangGraphAutonomousInputRouter(
        execution,
        None,
        NullAgentEventPublisher(),
        ports,
    )
    router.bind_turn_services(chat_service=None, coding_service=coding_service)
    return AgentService(ports, ports, router, ports)
@pytest.mark.asyncio
async def test_agent_service_binds_coding_runner_and_preserves_display_text():
    execution = FakeExecutionHost()
    coding = FakeCodingService()
    service = _agent_service_with_coding(execution, coding)
    context = TurnExecutionContext(thread_id="thread-1", session_id="session-1")

    await service.run_coding_turn(
        "generate agents",
        context=context,
        display_text="/init",
    )

    assert coding.calls == [
        {
            "user_text": "generate agents",
            "thread_id": "",
            "session_id": "session-1",
            "context": context,
            "display_text": "/init",
            "workspace": "/tmp/workspace",
        }
    ]


class FakeRuntime:
    def __init__(self) -> None:
        self.requests: list[TurnRequest] = []

    async def run_turn(self, request: TurnRequest):
        self.requests.append(request)


@pytest.mark.asyncio
async def test_agent_service_coding_runner_raises_without_coding_service():
    execution = FakeExecutionHost(session_id="session-1")
    runtime = FakeRuntime()
    service = _agent_service_with_coding(execution, None)

    with pytest.raises(RuntimeError, match="coding service is not configured"):
        await service.run_coding_turn(
            "generate agents",
            thread_id="thread-1",
            display_text="/init",
        )


@pytest.mark.asyncio
async def test_agent_service_coding_runner_preserves_explicit_context_identity():
    execution = FakeExecutionHost(session_id="active-session")
    runtime = FakeRuntime()
    service = _agent_service_with_coding(execution, CodingService(runtime))
    context = TurnExecutionContext(
        thread_id="target-thread",
        session_id="target-session",
        workspace="/tmp/workspace",
    )

    await service.run_coding_turn("continue target", context=context)

    assert len(runtime.requests) == 1
    request = runtime.requests[0]
    assert request.thread.thread_id == "target-thread"
    assert request.thread.session_id == "target-session"
    assert request.context is context
    assert request.context.workspace == "/tmp/workspace"


@pytest.mark.asyncio
async def test_agent_service_fallback_runner_preserves_explicit_context_identity():
    execution = FakeExecutionHost(session_id="active-session")
    runtime = FakeRuntime()
    service = _agent_service_with_coding(execution, None)
    context = TurnExecutionContext(
        thread_id="target-thread",
        session_id="target-session",
        workspace="/tmp/workspace",
    )

    with pytest.raises(RuntimeError, match="coding service is not configured"):
        await service.run_coding_turn("continue target", context=context)
