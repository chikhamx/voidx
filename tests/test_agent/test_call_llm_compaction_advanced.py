"""Tests for call_llm compaction and retry."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from rich.console import Console

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from voidx.agent.graph.streaming import stream_llm as _stream_llm
from voidx.agent.graph import VoidXGraph
from voidx.agent.graph.convergence import is_step_hint_message
from voidx.agent.runtime_context import RuntimeContextBuilder
from voidx.agent.task_state import TaskState, TodoRunState
from voidx.config import Config, ModelConfig
from voidx.llm.compaction import CompactionSelection
from voidx.llm.message_markers import is_guidance_message
from voidx.memory.context_frames import load_context_frames
from voidx.memory.session import MessageRow, create_session, delete_session, save_message
from voidx.ui.output.console import StreamingRenderer
from voidx.ui.output.dock import ANSI_LINE_PREFIX, BottomInputDock, set_dock
from voidx.ui.output.events import AnsiAppended, DockEventConsumer, StatusFinished, StatusUpdated, ui_events
from voidx.workflow.runtime import WorkflowRunState, WorkflowRunStatus
from tests.test_agent._stream_llm_helpers import (
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
    import voidx.agent.graph.core as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    graph = VoidXGraph(
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
        "max_steps": 50,
        "persona": "voidx",
    })

    assert result["messages"][0].content == "answer"
    assert model.bound_tools is not None


@pytest.mark.asyncio
async def test_call_llm_does_not_add_main_agent_step_hint(tmp_path, monkeypatch):
    import voidx.agent.graph.core as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    graph = VoidXGraph(
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
        "max_steps": 50,
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
    import voidx.agent.graph.core as graph_module

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)
    monkeypatch.setattr(graph_module.asyncio, "sleep", no_sleep)

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
        graph = VoidXGraph(
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
            "max_steps": 1,
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
        and event.label == "LLM error, retrying in 2s"
        and event.detail == "Connection error."
        for event in events
    )
    assert any(
        isinstance(event, StatusFinished)
        and event.status_id == "llm:retry"
        and event.remove is True
        for event in events
    )


