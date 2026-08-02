import asyncio
"""Tests for call_llm compaction and retry."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from rich.console import Console


from voidx.agent.infrastructure.langgraph.runtime.streaming import stream_llm as _stream_llm
from voidx.agent.infrastructure.langgraph.execution import LangGraphExecution
from voidx.agent.infrastructure.langgraph.runtime.convergence import is_step_hint_message
from voidx.agent.application.runtime_context import RuntimeContextBuilder
from voidx.runtime.task_state import TaskState, TodoRunState
from voidx.config import Config, ModelConfig
from voidx.llm.compaction import CompactionSelection
from voidx.llm.message_markers import is_guidance_message
from voidx.memory.context_frames import load_context_frames
from voidx.memory.session import MessageRow, create_session, delete_session, save_message
from voidx.ui.output.console import StreamingRenderer
from voidx.ui.output.dock import ANSI_LINE_PREFIX, BottomInputDock, set_dock
from voidx.ui.output.events import (
    AnsiAppended,
    AssistantStreamCommitted,
    AssistantStreamUpdated,
    DockEventConsumer,
    StatusFinished,
    StatusUpdated,
    ui_events,
)
from voidx.workflow.runtime import WorkflowRunState, WorkflowRunStatus
from tests.test_infrastructure.runtime.stream_llm_helpers import (
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
    import voidx.agent.infrastructure.langgraph.runtime.llm_turn as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    graph = LangGraphExecution(
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
    import voidx.agent.infrastructure.langgraph.runtime.llm_turn as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    graph = LangGraphExecution(
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
    import voidx.agent.infrastructure.langgraph.runtime.llm_turn as graph_module

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
        graph = LangGraphExecution(
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
    import voidx.agent.infrastructure.langgraph.runtime.llm_turn as graph_module

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
        graph = LangGraphExecution(
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
        isinstance(event, AssistantStreamUpdated)
        and "LLM call failed after 11 attempts: Connection error 11." in event.text
        for event in events
    )
    assert any(isinstance(event, AssistantStreamCommitted) for event in events)
