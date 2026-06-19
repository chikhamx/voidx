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
async def test_call_llm_resolves_protocol_for_mimo_provider(tmp_path, monkeypatch):
    import voidx.agent.graph.core.llm as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    graph = VoidXGraph(
        Config(
            model=ModelConfig(provider="mimo", model="mimo-v2.5"),
            workspace=str(tmp_path),
        ),
        api_key=None,
    )
    graph.model = FakeStreamingModel()

    result = await graph._call_llm({
        "messages": [HumanMessage(content="hi")],
        "step_count": 0,
        "max_steps": 1,
        "persona": "voidx",
    })

    assert result["step_count"] == 1
    assert result["messages"][0].content == "answer"


@pytest.mark.asyncio
async def test_call_llm_injects_current_todo_runtime_context(tmp_path, monkeypatch):
    import voidx.agent.graph.core.llm as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    graph = VoidXGraph(
        Config(
            model=ModelConfig(provider="mimo", model="mimo-v2.5"),
            workspace=str(tmp_path),
        ),
        api_key=None,
    )
    graph.model = FakeStreamingModel()
    graph._task_state.todo_state = TodoRunState.model_validate({
        "summary": "0/2 done · 1 active · 1 pending",
        "items": [
            {"content": "inspect todo replay", "status": "in_progress"},
            {"content": "write test", "status": "pending"},
        ],
    })

    messages = [HumanMessage(content="hi")]
    RuntimeContextBuilder(
        config=graph.config,
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode="auto",
        task_state=graph._task_state,
    ).build().apply_to_messages(messages)

    await graph._call_llm({
        "messages": messages,
        "step_count": 0,
        "max_steps": 50,
        "persona": "voidx",
    })

    todo_messages = [
        message.content
        for message in graph.model.messages
        if isinstance(message, HumanMessage) and "Active todo" in str(message.content)
    ]
    assert len(todo_messages) == 1
    assert "## Current Todo" not in todo_messages[0]
    assert "Active todo: 2 items" in todo_messages[0]
    assert "- in_progress: inspect todo replay" in todo_messages[0]


@pytest.mark.asyncio
async def test_call_llm_updates_usage_stats(tmp_path, monkeypatch):
    import voidx.agent.graph.core.llm as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    graph = VoidXGraph(
        Config(
            model=ModelConfig(provider="openai", model="gpt-4o"),
            workspace=str(tmp_path),
        ),
        api_key=None,
    )
    graph.model = FakeUsageStreamingModel()

    result = await graph._call_llm({
        "messages": [HumanMessage(content="hi")],
        "step_count": 0,
        "max_steps": 1,
        "persona": "voidx",
    })

    assert result["step_count"] == 1
    assert graph._usage_stats.last_input_tokens == 7
    assert graph._usage_stats.last_output_tokens == 3
    assert graph._usage_stats.total_input_tokens == 7
    assert graph._usage_stats.total_output_tokens == 3
    assert graph._usage_stats.total_calls == 1


@pytest.mark.asyncio
async def test_call_llm_persists_context_frame_for_session(tmp_path, monkeypatch):
    import voidx.agent.graph.core.llm as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)
    session = await create_session(workspace=str(tmp_path))
    try:
        user_message_id = await save_message(MessageRow(
            session_id=session.id,
            role="user",
            content="hi",
        ))
        graph = VoidXGraph(
            Config(
                model=ModelConfig(provider="mimo", model="mimo-v2.5"),
                workspace=str(tmp_path),
            ),
            api_key=None,
            session=session,
        )
        graph.model = FakeStreamingModel()

        await graph._call_llm({
            "messages": [
                SystemMessage(content=(
                    "VOIDX_RUNTIME_CONTEXT\n\n"
                    "## Base System\nbase\n\n"
                    "## Session Date\n2026-05-31 CST"
                )),
                HumanMessage(content="hi"),
            ],
            "step_count": 0,
            "max_steps": 1,
            "persona": "voidx",
            "user_message_id": user_message_id,
        })

        frames = await load_context_frames(session.id)
        assert len(frames) == 1
        assert frames[0].frame_kind == "main"
        assert frames[0].agent_persona == "voidx"
        assert frames[0].user_message_id == user_message_id
        assert frames[0].messages[-1]["content"] == "hi"
    finally:
        await delete_session(session.id)


