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
from voidx.agent.graph.compaction_coordinator import CompactionResult, PreflightCompactionResult
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
    AlwaysMalformedToolCallStreamingModel,
    RepairsMalformedToolCallStreamingModel,
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
        "total": 2,
        "done": 0,
        "active": 1,
        "pending": 1,
        "active_items": [
            {"id": "todo_replay", "content": "inspect todo replay", "status": "active"},
        ],
        "items": [
            {"id": "todo_replay", "content": "inspect todo replay", "status": "active"},
            {"id": "pending_item", "content": "pending work", "status": "pending"},
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
        "persona": "voidx",
    })

    todo_messages = [
        message.content
        for message in graph.model.messages
        if isinstance(message, HumanMessage) and "Todo:" in str(message.content)
    ]
    assert len(todo_messages) == 1
    assert "## Current Todo" not in todo_messages[0]
    assert "Todo: 0/2 done · 1 active · 1 pending" in todo_messages[0]
    assert "active: inspect todo replay" in todo_messages[0]
    assert "Active/Pending" not in todo_messages[0]


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


def test_inline_compaction_guide_disabled_by_default(tmp_path):
    graph = VoidXGraph(
        Config(
            model=ModelConfig(provider="mimo", model="mimo-v2.5"),
            workspace=str(tmp_path),
        ),
        api_key="test",
    )
    graph.config.inline_compaction_enabled = False
    graph._compaction.usable_window = lambda: 1
    graph._compaction.is_overflow = lambda _tokens: False
    graph._compaction.select_details = lambda _messages: CompactionSelection(
        head=[HumanMessage(content="old", id="old")],
        tail_id="current",
        keep_from=1,
        mode="normal",
    )

    guide = graph._inline_compaction_guide_for([
        HumanMessage(content="old", id="old"),
        AIMessage(content="old answer"),
        HumanMessage(content="current", id="current"),
    ])

    assert guide is None


@pytest.mark.asyncio
async def test_call_llm_overflow_compaction_does_not_send_temporary_summary_message(tmp_path, monkeypatch):
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
    overflow_results = iter([True, True, False, False])
    graph._compaction.is_overflow = lambda _tokens: next(overflow_results, False)
    graph._compaction.select_details = lambda messages: CompactionSelection(
        head=messages[:2],
        tail_id=getattr(messages[2], "id", None),
        keep_from=2,
        mode="normal",
    )

    async def summarize(_head_messages, _previous_summary):
        return "new compacted summary"

    graph._run_compaction_agent = summarize

    await graph._call_llm({
        "messages": [
            SystemMessage(content="VOIDX_RUNTIME_CONTEXT\n\n## Base System\nbase"),
            HumanMessage(content="old question", id="old_user"),
            AIMessage(content="old answer"),
            HumanMessage(content="previous question", id="previous_user"),
            AIMessage(content="previous answer"),
            HumanMessage(content="current question", id="current_user"),
        ],
        "step_count": 0,
        "persona": "voidx",
    })

    assert graph._compaction_summary == "new compacted summary"
    system_messages = [
        message
        for message in graph.model.messages
        if isinstance(message, SystemMessage)
    ]
    assert system_messages
    assert "## Long Summary\nnew compacted summary" in str(system_messages[0].content)
    assert not any(
        isinstance(message, SystemMessage)
        and isinstance(message.content, str)
        and message.content.startswith("## Long Summary")
        for message in graph.model.messages
    )


@pytest.mark.asyncio
async def test_call_llm_repairs_malformed_tool_call_once(tmp_path, monkeypatch):
    import voidx.agent.graph.core.llm as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)
    graph = VoidXGraph(
        Config(
            model=ModelConfig(provider="mimo", model="mimo-v2.5"),
            workspace=str(tmp_path),
        ),
        api_key=None,
    )
    graph.model = RepairsMalformedToolCallStreamingModel()

    result = await graph._call_llm({
        "messages": [HumanMessage(content="read the file")],
        "step_count": 0,
        "persona": "voidx",
    })

    assert graph.model.calls == 2
    assert result["messages"][0].content == "repaired answer"
    retry_messages = graph.model.messages_by_call[1]
    assert any(
        is_guidance_message(message)
        and "previous response looked like an incomplete tool call" in str(message.content)
        for message in retry_messages
    )
    assert not any(
        "<tool_call>" in str(getattr(message, "content", ""))
        for message in result["messages"]
    )


@pytest.mark.asyncio
async def test_call_llm_returns_explicit_error_when_malformed_tool_call_repair_fails(tmp_path, monkeypatch):
    import voidx.agent.graph.core.llm as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)
    graph = VoidXGraph(
        Config(
            model=ModelConfig(provider="mimo", model="mimo-v2.5"),
            workspace=str(tmp_path),
        ),
        api_key=None,
    )
    graph.model = AlwaysMalformedToolCallStreamingModel()

    result = await graph._call_llm({
        "messages": [HumanMessage(content="read the file")],
        "step_count": 0,
        "persona": "voidx",
    })

    assert graph.model.calls == 2
    assert result["should_continue"] is False
    assert result["messages"][0].content == (
        "LLM call failed: model returned an invalid or incomplete tool call."
    )


class MalformedThenRepairsAfterCompactionStreamingModel:
    def __init__(self) -> None:
        self.calls = 0
        self.messages_by_call = []

    def bind_tools(self, tool_defs):
        return self

    async def astream(self, messages):
        self.calls += 1
        self.messages_by_call.append(messages)
        if self.calls < 3:
            yield AIMessageChunk(content=(
                "<tool_call>"
                "<tool_name>read</tool_name>"
                "<arg_key>file_path</arg_key><arg_value>src/voidx/permission/engine.py</arg_value>"
            ))
            return
        yield AIMessageChunk(content="repaired after compaction")


@pytest.mark.asyncio
async def test_call_llm_runs_preflight_compaction_before_second_malformed_retry(tmp_path, monkeypatch):
    import voidx.agent.graph.core.llm as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)
    graph = VoidXGraph(
        Config(
            model=ModelConfig(provider="mimo", model="mimo-v2.5"),
            workspace=str(tmp_path),
        ),
        api_key="test-key",
    )
    graph.model = MalformedThenRepairsAfterCompactionStreamingModel()
    compaction_reasons: list[str] = []
    graph._compaction.is_overflow = lambda _tokens: len(compaction_reasons) == 0

    async def preflight(messages, session_msgs=None, *, force=False, reason="threshold", ask=False):
        compaction_reasons.append(reason)
        tail = list(messages[2:]) if reason == "hard_threshold" else list(messages)
        tail_id = getattr(tail[0], "id", None) if tail else None
        result = CompactionResult(
            summary="",
            removed_messages=list(messages[:2]) if reason == "hard_threshold" else [],
            live_messages=tail,
            tail_id=tail_id,
            metadata={"compaction_reason": reason},
        )
        return result, PreflightCompactionResult.from_compaction_result(result)

    graph._preflight_compact_if_needed = preflight

    result = await graph._call_llm({
        "messages": [
            HumanMessage(content="old question", id="old_user"),
            AIMessage(content="old answer"),
            HumanMessage(content="current question", id="current_user"),
        ],
        "step_count": 0,
        "persona": "voidx",
    })

    assert graph.model.calls == 3
    assert compaction_reasons == ["hard_threshold", "malformed_tool_call"]
    assistant_messages = [
        message for message in result["messages"] if isinstance(message, AIMessage)
    ]
    assert assistant_messages[-1].content == "repaired after compaction"
    final_retry_messages = graph.model.messages_by_call[2]
    assert any(
        is_guidance_message(message)
        and "previous response looked like an incomplete tool call" in str(message.content)
        for message in final_retry_messages
    )
    assert not any(
        "old question" in str(getattr(message, "content", ""))
        for message in final_retry_messages
    )


class AlwaysMalformedWithCompactionStreamingModel:
    def __init__(self) -> None:
        self.calls = 0

    def bind_tools(self, tool_defs):
        return self

    async def astream(self, messages):
        self.calls += 1
        yield AIMessageChunk(content=(
            "<tool_call>"
            "<tool_name>read</tool_name>"
            "<arg_key>file_path</arg_key><arg_value>src/voidx/permission/engine.py</arg_value>"
        ))


@pytest.mark.asyncio
async def test_call_llm_returns_explicit_error_after_malformed_compaction_retry_fails(tmp_path, monkeypatch):
    import voidx.agent.graph.core.llm as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)
    graph = VoidXGraph(
        Config(
            model=ModelConfig(provider="mimo", model="mimo-v2.5"),
            workspace=str(tmp_path),
        ),
        api_key="test-key",
    )
    graph.model = AlwaysMalformedWithCompactionStreamingModel()
    compaction_reasons: list[str] = []
    graph._compaction.is_overflow = lambda _tokens: len(compaction_reasons) == 0

    async def preflight(messages, session_msgs=None, *, force=False, reason="threshold", ask=False):
        compaction_reasons.append(reason)
        tail = list(messages[2:]) if reason == "hard_threshold" else list(messages)
        tail_id = getattr(tail[0], "id", None) if tail else None
        result = CompactionResult(
            summary="",
            removed_messages=list(messages[:2]) if reason == "hard_threshold" else [],
            live_messages=tail,
            tail_id=tail_id,
            metadata={"compaction_reason": reason},
        )
        return result, PreflightCompactionResult.from_compaction_result(result)

    graph._preflight_compact_if_needed = preflight

    result = await graph._call_llm({
        "messages": [
            HumanMessage(content="old question", id="old_user"),
            AIMessage(content="old answer"),
            HumanMessage(content="current question", id="current_user"),
        ],
        "step_count": 0,
        "persona": "voidx",
    })

    assert graph.model.calls == 3
    assert compaction_reasons == ["hard_threshold", "malformed_tool_call"]
    assert result["should_continue"] is False
    assistant_messages = [
        message for message in result["messages"] if isinstance(message, AIMessage)
    ]
    assert assistant_messages[-1].content == (
        "LLM call failed: model returned an invalid or incomplete tool call."
    )
    assert not any(
        "<tool_call>" in str(getattr(message, "content", ""))
        for message in result["messages"]
    )