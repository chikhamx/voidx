"""Tests for call_llm compaction and retry."""

import sys
import warnings
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
from voidx.agent.adapters.langgraph.runtime.compaction_coordinator import CompactionResult, PreflightCompactionResult
from voidx.llm.compaction import CompactionSelection, SUMMARY_TEMPLATE
from voidx.llm.usage import estimate_context_tokens
from voidx.llm.message_markers import is_guidance_message
from voidx.agent.adapters.persistence.context_frame_repository import load_context_frames
from voidx.agent.adapters.persistence.session_repository import MessageRow, create_session, delete_session, save_message
from voidx.presentation.output.console import StreamingRenderer
from voidx.presentation.output.dock import ANSI_LINE_PREFIX, BottomInputDock, set_dock
from voidx.presentation.output.events import AnsiAppended, DockEventConsumer, StatusFinished, StatusUpdated, ui_events
from voidx.agent.application.automation.workflow.runtime import WorkflowRunState, WorkflowRunStatus
from tests.test_agent.adapters.langgraph.runtime.stream_llm_helpers import (
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
    FailsNonRetryableStreamingModel,
    FakeRenderer,
)

@pytest.mark.asyncio
async def test_call_llm_resolves_protocol_for_mimo_provider(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    graph = make_langgraph_execution(
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
async def test_main_agent_hard_context_pressure_keeps_tools_and_does_not_force_final(
    tmp_path,
    monkeypatch,
):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module
    from voidx.agent.adapters.langgraph.runtime.context_pressure import ContextPressureDecision
    from voidx.llm.message_markers import is_context_pressure_message

    class ToolRecordingModel(FakeStreamingModel):
        def __init__(self) -> None:
            super().__init__()
            self.bound_tools = None

        def bind_tools(self, tool_defs):
            self.bound_tools = list(tool_defs)
            return self

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)
    monkeypatch.setattr(
        graph_module,
        "evaluate_context_pressure",
        lambda *_args, **_kwargs: ContextPressureDecision(
            over_soft=True,
            over_hard=True,
            can_compact=False,
            pressure_level="hard",
            should_inject=True,
            turn_id="turn-1",
            turn_count=1,
            pre_tokens=90_000,
            soft_threshold=75_000,
            hard_threshold=90_000,
            reason="hard_threshold",
        ),
    )
    graph = make_langgraph_execution(
        Config(
            model=ModelConfig(provider="mimo", model="mimo-v2.5"),
            workspace=str(tmp_path),
        ),
        api_key="test",
    )
    graph.model = ToolRecordingModel()

    result = await graph._call_llm({
        "messages": [HumanMessage(id="turn-1", content="finish the task")],
        "step_count": 10,
        "persona": "coordinate",
    })

    assert graph.model.bound_tools
    pressure_hints = [
        message for message in graph.model.messages
        if is_context_pressure_message(message)
    ]
    assert len(pressure_hints) == 1
    assert pressure_hints[0].additional_kwargs["pressure_level"] == "hard"
    assert result["convergence_forced"] is False




@pytest.mark.asyncio
async def test_main_hard_compaction_failure_injects_hint_and_keeps_tools(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module
    from voidx.agent.adapters.langgraph.runtime.context_pressure import ContextPressureDecision
    from voidx.llm.message_markers import is_context_pressure_message

    class ToolRecordingModel(FakeStreamingModel):
        def __init__(self) -> None:
            super().__init__()
            self.bound_tools = None

        def bind_tools(self, tool_defs):
            self.bound_tools = list(tool_defs)
            return self

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)
    monkeypatch.setattr(
        graph_module,
        "evaluate_context_pressure",
        lambda *_args, **_kwargs: ContextPressureDecision(
            over_soft=True,
            over_hard=True,
            can_compact=True,
            pressure_level="hard",
            should_inject=False,
            turn_id="turn-hard",
            turn_count=2,
            pre_tokens=90_000,
            soft_threshold=75_000,
            hard_threshold=90_000,
            reason="hard_threshold",
        ),
    )
    graph = make_langgraph_execution(
        Config(
            model=ModelConfig(provider="mimo", model="mimo-v2.5"),
            workspace=str(tmp_path),
        ),
        api_key="test-key",
    )
    graph.model = ToolRecordingModel()
    graph._compaction.is_overflow = lambda _tokens: True

    async def failed_preflight(*_args, **_kwargs):
        return None, None

    graph._preflight_compact_if_needed = failed_preflight

    result = await graph._call_llm({
        "messages": [HumanMessage(id="turn-hard", content="finish the task")],
        "step_count": 10,
        "persona": "coordinate",
    })

    pressure_hints = [
        message for message in graph.model.messages
        if is_context_pressure_message(message)
    ]
    assert len(pressure_hints) == 1
    assert pressure_hints[0].additional_kwargs["pressure_level"] == "hard"
    assert graph.model.bound_tools
    assert result["convergence_forced"] is False


@pytest.mark.asyncio
async def test_provider_overflow_hard_hint_saves_retry_context_frame(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module
    from voidx.llm.message_markers import is_context_pressure_message

    class OverflowOnceModel(FakeStreamingModel):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0
            self.messages_by_call = []

        async def astream(self, messages):
            self.calls += 1
            self.messages_by_call.append(list(messages))
            if self.calls == 1:
                if False:
                    yield AIMessageChunk(content="")
                error = RuntimeError("context length exceeded")
                error.status_code = 400  # type: ignore[attr-defined]
                raise error
            async for chunk in super().astream(messages):
                yield chunk

    saved_frames: list[dict] = []

    async def capture_frame(**kwargs):
        saved_frames.append(kwargs)

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)
    monkeypatch.setattr(graph_module, "save_main_context_frame", capture_frame)
    monkeypatch.setattr(
        graph_module,
        "estimate_context_tokens_with_tools",
        lambda messages, _tools, _model: (
            222 if any(is_context_pressure_message(message) for message in messages) else 111
        ),
    )
    graph = make_langgraph_execution(
        Config(
            model=ModelConfig(provider="mimo", model="mimo-v2.5"),
            workspace=str(tmp_path),
        ),
        api_key="test-key",
    )
    graph.model = OverflowOnceModel()
    graph._compaction.is_overflow = lambda _tokens: False

    async def failed_preflight(*_args, **_kwargs):
        return None, None

    graph._preflight_compact_if_needed = failed_preflight

    await graph._call_llm({
        "messages": [HumanMessage(id="turn-overflow", content="finish the task")],
        "step_count": 0,
        "persona": "coordinate",
    })

    assert graph.model.calls == 2
    assert len(saved_frames) == 2
    assert saved_frames[1]["messages"] == graph.model.messages_by_call[1]
    assert saved_frames[1]["token_estimate"] == 222
    assert any(is_context_pressure_message(message) for message in saved_frames[1]["messages"])
    assert saved_frames[1]["convergence_forced"] is False
@pytest.mark.asyncio
async def test_call_llm_injects_current_todo_runtime_context(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    graph = make_langgraph_execution(
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
async def test_call_llm_updates_usage_stats_across_turn_control_calls(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    class TurnControlUsageStreamingModel:
        def __init__(self):
            self.calls = 0

        def bind_tools(self, _tool_defs):
            return self

        async def astream(self, _messages):
            self.calls += 1
            if self.calls == 1:
                yield AIMessageChunk(
                    content="answer",
                    usage_metadata={
                        "input_tokens": 7,
                        "output_tokens": 3,
                        "total_tokens": 10,
                    },
                )
                return
            if self.calls == 2:
                yield AIMessageChunk(
                    content="",
                    tool_calls=[
                        {
                            "name": "turn",
                            "args": {"operation": "stop", "params": None},
                            "id": "turn-usage",
                            "type": "tool_call",
                        }
                    ],
                    usage_metadata={
                        "input_tokens": 2,
                        "output_tokens": 1,
                        "total_tokens": 3,
                    },
                )
                return
            pytest.fail(f"Unexpected LLM call {self.calls}")

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    graph = make_langgraph_execution(
        Config(
            model=ModelConfig(provider="openai", model="gpt-4o"),
            workspace=str(tmp_path),
        ),
        api_key=None,
    )
    graph.model = TurnControlUsageStreamingModel()

    result = await graph._call_llm({
        "messages": [HumanMessage(content="hi")],
        "step_count": 0,
        "persona": "voidx",
        "turn_state": "running",
    })

    assert result["step_count"] == 1
    assert result["messages"][0].content == "answer"
    assert graph.model.calls == 2
    assert graph._usage_stats.last_input_tokens == 2
    assert graph._usage_stats.last_output_tokens == 1
    assert graph._usage_stats.context_tokens == 2
    assert graph._usage_stats.total_input_tokens == 9
    assert graph._usage_stats.total_output_tokens == 4
    assert graph._usage_stats.total_calls == 2


@pytest.mark.asyncio
async def test_call_llm_fallback_context_estimate_includes_tool_schema(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    graph = make_langgraph_execution(
        Config(
            model=ModelConfig(provider="openai", model="gpt-4o"),
            workspace=str(tmp_path),
        ),
        api_key=None,
    )
    graph.model = FakeStreamingModel()

    await graph._call_llm({
        "messages": [HumanMessage(content="hi")],
        "step_count": 0,
        "persona": "voidx",
        "turn_state": "running",
    })

    messages_only = estimate_context_tokens(graph.model.messages, graph.config.model.model)
    assert graph._usage_stats.context_tokens > messages_only


@pytest.mark.asyncio
async def test_call_llm_persists_context_frame_for_session(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)
    session = await create_session(workspace=str(tmp_path))
    try:
        user_message_id = await save_message(MessageRow(
            session_id=session.id,
            role="user",
            content="hi",
        ))
        graph = make_langgraph_execution(
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
    graph = make_langgraph_execution(
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



def test_inline_compaction_guide_uses_shared_summary_template(tmp_path):
    graph = make_langgraph_execution(
        Config(
            model=ModelConfig(provider="mimo", model="mimo-v2.5"),
            workspace=str(tmp_path),
        ),
        api_key=None,
    )
    graph.config.inline_compaction_enabled = True
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

    assert guide is not None
    assert SUMMARY_TEMPLATE in str(guide.content)
    assert "tail_anchor_id: current" in str(guide.content)

@pytest.mark.asyncio
async def test_call_llm_overflow_compaction_does_not_send_temporary_summary_message(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)
    graph = make_langgraph_execution(
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
async def test_call_llm_coerces_todo_state_dict_before_compaction_dump(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)
    graph = make_langgraph_execution(
        Config(
            model=ModelConfig(provider="mimo", model="mimo-v2.5"),
            workspace=str(tmp_path),
        ),
        api_key=None,
    )
    graph.model = FakeStreamingModel()
    graph._compaction.is_overflow = lambda _tokens: True

    async def preflight(messages, session_msgs=None, *, force=False, reason="threshold", ask=False):
        result = CompactionResult(
            summary="compacted",
            removed_messages=list(messages[:1]),
            live_messages=list(messages[1:]),
            tail_id=getattr(messages[1], "id", None) if len(messages) > 1 else None,
            metadata={"compaction_reason": reason},
        )
        return result, PreflightCompactionResult.from_compaction_result(result)

    graph._preflight_compact_if_needed = preflight

    todo_state = {
        "summary": "0/1 done · 1 active · 0 pending",
        "total": 1,
        "done": 0,
        "active": 1,
        "pending": 0,
        "active_items": [
            {"id": "inspect", "content": "inspect warning", "status": "active"},
        ],
        "items": [
            {"id": "inspect", "content": "inspect warning", "status": "active"},
        ],
    }

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await graph._call_llm({
            "messages": [
                HumanMessage(content="old question", id="old_user"),
                AIMessage(content="old answer"),
                HumanMessage(content="current question", id="current_user"),
            ],
            "step_count": 0,
            "persona": "voidx",
            "todo_state": todo_state,
        })

    assert not any(
        "PydanticSerializationUnexpectedValue" in str(warning.message)
        for warning in caught
    )


@pytest.mark.asyncio
async def test_call_llm_repairs_malformed_tool_call_once(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)
    graph = make_langgraph_execution(
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
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)
    graph = make_langgraph_execution(
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
        yield AIMessageChunk(
            content="",
            tool_calls=[{"name": "turn", "args": {"operation": "stop", "params": None}, "id": "turn-1", "type": "tool_call"}],
        )


@pytest.mark.asyncio
async def test_call_llm_runs_preflight_compaction_before_second_malformed_retry(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)
    graph = make_langgraph_execution(
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
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)
    graph = make_langgraph_execution(
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

@pytest.mark.asyncio
async def test_classify_llm_error_404_fail_fast(tmp_path, monkeypatch):
    """404 model_not_found → NON_RETRYABLE, should not retry."""
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module
    from voidx.agent.adapters.langgraph.runtime.core.helpers import _classify_llm_error, LLMErrorKind

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    exc = RuntimeError("404 model_not_found")
    exc.status_code = 404  # type: ignore

    kind = _classify_llm_error(exc)
    assert kind == LLMErrorKind.NON_RETRYABLE


@pytest.mark.asyncio
async def test_classify_llm_error_429_rate_limit(tmp_path, monkeypatch):
    """429 rate limit → RATE_LIMIT, should retry."""
    import voidx.agent.adapters.langgraph.runtime.core.helpers as helpers

    exc = RuntimeError("rate limit")
    exc.status_code = 429  # type: ignore

    kind = helpers._classify_llm_error(exc)
    assert kind == helpers.LLMErrorKind.RATE_LIMIT


@pytest.mark.asyncio
async def test_classify_llm_error_400_context_overflow(tmp_path, monkeypatch):
    """400 with context overflow → CONTEXT_OVERFLOW, should compact."""
    import voidx.agent.adapters.langgraph.runtime.core.helpers as helpers

    exc = RuntimeError("context_length_exceeded")
    exc.status_code = 400  # type: ignore

    kind = helpers._classify_llm_error(exc)
    assert kind == helpers.LLMErrorKind.CONTEXT_OVERFLOW


@pytest.mark.asyncio
async def test_classify_llm_error_400_non_overflow(tmp_path, monkeypatch):
    """400 without overflow → NON_RETRYABLE."""
    import voidx.agent.adapters.langgraph.runtime.core.helpers as helpers

    exc = RuntimeError("bad request: invalid model")
    exc.status_code = 400  # type: ignore

    kind = helpers._classify_llm_error(exc)
    assert kind == helpers.LLMErrorKind.NON_RETRYABLE


@pytest.mark.asyncio
async def test_classify_llm_error_503_schema(tmp_path, monkeypatch):
    """503 with schema error → NON_RETRYABLE."""
    import voidx.agent.adapters.langgraph.runtime.core.helpers as helpers

    exc = RuntimeError("invalid schema for function 'weather'")
    exc.status_code = 503  # type: ignore

    kind = helpers._classify_llm_error(exc)
    assert kind == helpers.LLMErrorKind.NON_RETRYABLE


@pytest.mark.asyncio
async def test_classify_llm_error_503_server_error(tmp_path, monkeypatch):
    """503 without schema error → SERVER_ERROR, should retry."""
    import voidx.agent.adapters.langgraph.runtime.core.helpers as helpers

    exc = RuntimeError("service temporarily unavailable")
    exc.status_code = 503  # type: ignore

    kind = helpers._classify_llm_error(exc)
    assert kind == helpers.LLMErrorKind.SERVER_ERROR


@pytest.mark.asyncio
async def test_classify_llm_error_connection_error(tmp_path, monkeypatch):
    """ConnectionError → NETWORK, should retry."""
    import voidx.agent.adapters.langgraph.runtime.core.helpers as helpers

    exc = ConnectionError("Connection refused")

    kind = helpers._classify_llm_error(exc)
    assert kind == helpers.LLMErrorKind.NETWORK


@pytest.mark.asyncio
async def test_classify_llm_error_timeout(tmp_path, monkeypatch):
    """TimeoutError → TIMEOUT, should retry."""
    import voidx.agent.adapters.langgraph.runtime.core.helpers as helpers

    import asyncio
    exc = asyncio.TimeoutError("timed out")

    kind = helpers._classify_llm_error(exc)
    assert kind == helpers.LLMErrorKind.TIMEOUT


@pytest.mark.asyncio
async def test_classify_llm_error_unknown(tmp_path, monkeypatch):
    """Unknown error → UNKNOWN, should retry (conservative)."""
    import voidx.agent.adapters.langgraph.runtime.core.helpers as helpers

    exc = RuntimeError("something weird happened")

    kind = helpers._classify_llm_error(exc)
    assert kind == helpers.LLMErrorKind.UNKNOWN



@pytest.mark.asyncio
async def test_call_llm_unknown_programming_error_is_raised_without_retry(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    class ProgrammingErrorModel(FakeStreamingModel):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def astream(self, _messages):
            self.calls += 1
            if False:
                yield AIMessageChunk(content="")
            raise ValueError("programming defect")

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)
    graph = make_langgraph_execution(
        Config(
            model=ModelConfig(provider="openai", model="gpt-4o"),
            workspace=str(tmp_path),
        ),
        api_key="test-key",
    )
    graph.model = ProgrammingErrorModel()

    with pytest.raises(ValueError, match="programming defect"):
        await graph._call_llm({
            "messages": [HumanMessage(content="hi")],
            "step_count": 0,
            "persona": "voidx",
        })

    assert graph.model.calls == 1
@pytest.mark.asyncio
async def test_call_llm_non_retryable_404_fail_fast(tmp_path, monkeypatch):
    """A 404 error should fail-fast without retrying."""
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    graph = make_langgraph_execution(
        Config(
            model=ModelConfig(provider="openai", model="gpt-4o"),
            workspace=str(tmp_path),
        ),
        api_key=None,
    )
    graph.model = FailsNonRetryableStreamingModel()

    result = await graph._call_llm({
        "messages": [HumanMessage(content="hi")],
        "step_count": 0,
        "persona": "voidx",
    })

    # Should fail fast — one attempt, no retries
    assert graph.model.calls == 1
    assert result["should_continue"] is False
    assert result["step_count"] == 0
    assert result["messages"] == []


def test_llm_retry_delay_schedule():
    """Verify the two-phase delay schedule for all 10 retry attempts."""
    from voidx.agent.adapters.langgraph.runtime.core.helpers import _llm_retry_delay

    delays = [_llm_retry_delay(i) for i in range(1, 11)]
    assert delays == [2.0, 2.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0]


def test_llm_retry_delay_fixed_phase():
    """First two retries use fixed delay regardless of attempt number."""
    from voidx.agent.adapters.langgraph.runtime.core.helpers import _llm_retry_delay

    assert _llm_retry_delay(1) == 2.0
    assert _llm_retry_delay(2) == 2.0


def test_llm_retry_delay_exponential_cap():
    """Exponential phase doubles until capped at 60s."""
    from voidx.agent.adapters.langgraph.runtime.core.helpers import _llm_retry_delay

    assert _llm_retry_delay(3) == 2.0
    assert _llm_retry_delay(4) == 4.0
    assert _llm_retry_delay(5) == 8.0
    assert _llm_retry_delay(6) == 16.0
    assert _llm_retry_delay(7) == 32.0
    assert _llm_retry_delay(8) == 60.0
    assert _llm_retry_delay(9) == 60.0
    assert _llm_retry_delay(10) == 60.0


def test_clean_error_message():
    from voidx.agent.adapters.langgraph.runtime.core.helpers import _clean_error_message

    # Test case 1: OpenAI rate limit error with dict representation
    exc1 = Exception("Error code: 402 - {'error': {'type': 'rate_limit_error', 'message': '每日额度超限: 当前 $50.813...'}}")
    assert _clean_error_message(exc1) == "Error code: 402 - 每日额度超限: 当前 $50.813..."

    # Test case 2: OpenAI error with json representation
    exc2 = Exception('Error code: 400 - {"error": {"message": "Invalid prompt", "type": "invalid_request_error"}}')
    assert _clean_error_message(exc2) == "Error code: 400 - Invalid prompt"

    # Test case 3: Standard exception with no JSON/dict
    exc3 = Exception("Connection refused")
    assert _clean_error_message(exc3) == "Connection refused"

    # Test case 4: Dict representation without error key
    exc4 = Exception("Error: {'message': 'Something went wrong'}")
    assert _clean_error_message(exc4) == "Error - Something went wrong"
