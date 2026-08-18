import asyncio
"""Tests for call_llm compaction and retry."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from rich.console import Console


from voidx.agent.adapters.langgraph.runtime.streaming import stream_llm as _stream_llm
from voidx.agent.adapters.langgraph.execution import LangGraphExecution
from tests.langgraph_execution import make_langgraph_execution
from voidx.agent.adapters.langgraph.runtime.convergence import is_step_hint_message
from voidx.agent.application.runtime_context import RuntimeContextBuilder
from voidx.agent.domain.task.state import TaskState
from voidx.agent.domain.task.todo import TodoRunState
from voidx.config import Config
from voidx.llm.domain.model import ModelConfig
from voidx.llm.compaction import CompactionSelection
from voidx.llm.message_markers import is_guidance_message
from voidx.agent.adapters.persistence.context_frame_repository import load_context_frames
from voidx.agent.adapters.persistence.session_repository import MessageRow, create_session, delete_session, save_message
from voidx.presentation.output.console import StreamingRenderer
from voidx.presentation.output.dock import ANSI_LINE_PREFIX, BottomInputDock, set_dock
from voidx.presentation.output.events import (
    AnsiAppended,
    ErrorAppended,
    DockEventConsumer,
    StatusFinished,
    StatusUpdated,
    ui_events,
)
from voidx.agent.application.automation.workflow.runtime import WorkflowRunState, WorkflowRunStatus
from tests.test_agent.adapters.langgraph.runtime.stream_llm_helpers import (
    _plain,
    FakeStreamingModel,
    FakeUsageStreamingModel,
    FakeDuplicatedReasoningStreamingModel,
    FakeDsmlStreamingModel,
    FakeMalformedDsmlStreamingModel,
    TrackingStreamingModel,
    FailsOnceStreamingModel,
    FakeRenderer,
)


@pytest.mark.asyncio
async def test_call_llm_ignores_legacy_max_steps_for_tool_binding(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    graph = make_langgraph_execution(
        Config(
            model=ModelConfig(provider="mimo", model="mimo-v2.5"),
            workspace=str(tmp_path),
        ),
        api_key=None,
    )
    model = TrackingStreamingModel()
    graph.model = model

    result = await graph._call_llm({
        "messages": [HumanMessage(content="hi")],
        "step_count": 49,
        "persona": "voidx",
    })

    assert result["messages"][0].content == "answer"
    assert model.bound_tools is not None


@pytest.mark.asyncio
async def test_call_llm_does_not_add_main_agent_step_hint(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    graph = make_langgraph_execution(
        Config(
            model=ModelConfig(provider="mimo", model="mimo-v2.5"),
            workspace=str(tmp_path),
        ),
        api_key=None,
    )
    model = TrackingStreamingModel()
    graph.model = model
    state_messages = [HumanMessage(content="finish the task")]

    result = await graph._call_llm({
        "messages": state_messages,
        "step_count": 46,
        "persona": "voidx",
    })

    assert result["messages"][0].content == "answer"
    assert result["convergence_forced"] is False
    assert len(state_messages) == 1
    assert not any(is_step_hint_message(message) for message in result["messages"])
    assert model.messages is not None
    assert len(model.messages) == 1
    assert not any(is_step_hint_message(message) for message in model.messages)


@pytest.mark.asyncio
async def test_call_llm_retry_uses_transient_status_event(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    events: list[object] = []

    class RecordingConsumer:
        def handle(self, event):
            events.append(event)
            return None

    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    ui_events.start(RecordingConsumer())
    try:
        graph = make_langgraph_execution(
            Config(
                model=ModelConfig(provider="mimo", model="mimo-v2.5"),
                workspace=str(tmp_path),
            ),
            api_key=None,
        )
        graph.model = FailsOnceStreamingModel()

        result = await graph._call_llm({
            "messages": [HumanMessage(content="hi")],
            "step_count": 0,
            "persona": "voidx",
        })
        await ui_events.drain()
    finally:
        await ui_events.stop()
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)

    assert result["messages"][0].content == "answer"
    assert not any(
        isinstance(event, AnsiAppended) and "LLM error, retrying" in event.text
        for event in events
    )
    assert any(
        isinstance(event, StatusUpdated)
        and event.status_id == "llm:retry"
        and event.label == "Retrying"
        and event.detail == "retrying in 2s: Connection error."
        for event in events
    )
    assert any(
        isinstance(event, StatusFinished)
        and event.status_id == "llm:retry"
        and event.remove is True
        for event in events
    )




class AlwaysFailsStreamingModel:
    def __init__(self) -> None:
        self.calls = 0

    def bind_tools(self, tool_defs):
        return self

    async def astream(self, messages):
        self.calls += 1
        raise ConnectionError(f"Connection error {self.calls}.")
        yield AIMessageChunk(content="")


@pytest.mark.asyncio
async def test_call_llm_exhausts_retries_then_renders_assistant_error(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    events: list[object] = []

    class RecordingConsumer:
        def handle(self, event):
            events.append(event)
            return None

    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    ui_events.start(RecordingConsumer())
    try:
        graph = make_langgraph_execution(
            Config(
                model=ModelConfig(provider="mimo", model="mimo-v2.5"),
                workspace=str(tmp_path),
            ),
            api_key="test-key",
        )
        graph.model = AlwaysFailsStreamingModel()

        result = await graph._call_llm({
            "messages": [HumanMessage(content="hi")],
            "step_count": 0,
            "persona": "voidx",
        })
        await ui_events.drain()
    finally:
        await ui_events.stop()
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)

    assert graph.model.calls == 11
    assert result["should_continue"] is False
    assert result.get("messages", []) == []
    retry_events = [
        event for event in events
        if isinstance(event, StatusUpdated) and event.status_id == "llm:retry"
    ]
    assert [event.label for event in retry_events] == ["Retrying"] * 10
    assert [event.detail for event in retry_events] == [
        "retrying in 2s: Connection error 1.",
        "retrying in 2s: Connection error 2.",
        "retrying in 2s: Connection error 3.",
        "retrying in 4s: Connection error 4.",
        "retrying in 8s: Connection error 5.",
        "retrying in 16s: Connection error 6.",
        "retrying in 32s: Connection error 7.",
        "retrying in 60s: Connection error 8.",
        "retrying in 60s: Connection error 9.",
        "retrying in 60s: Connection error 10.",
    ]
    assert any(
        isinstance(event, ErrorAppended)
        and event.message == "LLM call failed after 11 attempts: Connection error 11."
        for event in events
    )


@pytest.mark.asyncio
async def test_non_retryable_llm_error_is_emitted_as_error_event():
    from voidx.agent.adapters.langgraph.runtime.core.helpers import LLMErrorKind
    from voidx.agent.adapters.langgraph.runtime.core.loop import (
        LlmLoopState,
        handle_llm_exception,
    )

    class RecordingEvents:
        def __init__(self) -> None:
            self.events = []

        async def emit(self, event) -> bool:
            self.events.append(event)
            return True

    class EventUi:
        def __init__(self) -> None:
            self.events = RecordingEvents()

        def via_events(self) -> bool:
            return True

    ui = EventUi()
    result = await handle_llm_exception(
        ui=ui,
        loop=LlmLoopState(context_tokens=0),
        error=Exception("Error code: 403 - authorization failed"),
        kind=LLMErrorKind.NON_RETRYABLE,
        max_retries=10,
        timeout_max_retries=1,
    )

    assert result.action == "fail"
    assert [type(event) for event in ui.events.events] == [
        StatusUpdated,
        StatusFinished,
        ErrorAppended,
    ]
    assert ui.events.events[-1].message == (
        "LLM call failed (non-retryable): Error code: 403 - authorization failed"
    )


@pytest.mark.asyncio
async def test_terminal_llm_error_prefers_error_event_when_event_bus_is_available():
    from voidx.agent.adapters.langgraph.runtime.core.helpers import LLMErrorKind
    from voidx.agent.adapters.langgraph.runtime.core.loop import (
        LlmLoopState,
        handle_llm_exception,
    )

    class RecordingEvents:
        def __init__(self) -> None:
            self.events = []

        async def emit(self, event) -> bool:
            self.events.append(event)
            return True

    class EventUi:
        def __init__(self) -> None:
            self.events = RecordingEvents()
            self.errors = []
            self.ui = self

        def via_events(self) -> bool:
            return False

        def error(self, message: str) -> None:
            self.errors.append(message)

    ui = EventUi()
    await handle_llm_exception(
        ui=ui,
        loop=LlmLoopState(context_tokens=0),
        error=Exception("authorization failed"),
        kind=LLMErrorKind.NON_RETRYABLE,
        max_retries=10,
        timeout_max_retries=1,
    )

    assert [type(event) for event in ui.events.events] == [ErrorAppended]
    assert ui.events.events[0].message == (
        "LLM call failed (non-retryable): authorization failed"
    )
    assert ui.errors == []


@pytest.mark.asyncio
async def test_terminal_llm_error_falls_back_when_event_bus_is_unavailable():
    from voidx.agent.adapters.langgraph.runtime.core.helpers import LLMErrorKind
    from voidx.agent.adapters.langgraph.runtime.core.loop import (
        LlmLoopState,
        handle_llm_exception,
    )

    class UnavailableEvents:
        async def emit(self, event) -> bool:
            return False

    class EventUi:
        def __init__(self) -> None:
            self.events = UnavailableEvents()
            self.errors = []
            self.ui = self

        def via_events(self) -> bool:
            return False

        def error(self, message: str) -> None:
            self.errors.append(message)

    ui = EventUi()
    await handle_llm_exception(
        ui=ui,
        loop=LlmLoopState(context_tokens=0),
        error=Exception("authorization failed"),
        kind=LLMErrorKind.NON_RETRYABLE,
        max_retries=10,
        timeout_max_retries=1,
    )

    assert ui.errors == [
        "LLM call failed (non-retryable): authorization failed"
    ]
