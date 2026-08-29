"""Tests for stream LLM sanitization, DSML, and replay."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from rich.console import Console


from voidx.agent.adapters.langgraph.runtime.streaming import stream_llm as _stream_llm
from voidx.agent.adapters.langgraph.execution import LangGraphExecution
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
from voidx.presentation.output.events import AnsiAppended, DockEventConsumer, StatusFinished, StatusUpdated, ui_events
from voidx.agent.application.automation.workflow.runtime import WorkflowRunState, WorkflowRunStatus
from tests.presentation_ui import make_presentation_ui
from tests.test_agent.adapters.langgraph.runtime.stream_llm_helpers import (
    _plain,
    FakeStreamingModel,
    FakeUsageStreamingModel,
    FakeDuplicatedReasoningStreamingModel,
    FakeDsmlStreamingModel,
    FakeMalformedDsmlStreamingModel,
    FakeMalformedLegacyXmlStreamingModel,
    FakeMalformedProviderJsonToolCallStreamingModel,
    FakeLegacyXmlToolCallStreamingModel,
    FakeLegacyXmlArgPairToolCallStreamingModel,
    TrackingStreamingModel,
    FailsOnceStreamingModel,
    FakeRenderer,
)

@pytest.mark.asyncio
async def test_stream_llm_uses_protocol_for_thinking_extraction():
    renderer = FakeRenderer()

    msg = await _stream_llm(FakeStreamingModel(), [], renderer, "anthropic")

    assert msg.content == "answer"
    assert renderer.started is True
    assert renderer.done_called is True
    assert renderer.discarded is False
    assert renderer.text == ["answer"]
    assert renderer.thinking == ["think"]


@pytest.mark.asyncio
async def test_stream_llm_hides_duplicated_reasoning_content():
    renderer = FakeRenderer()

    msg = await _stream_llm(FakeDuplicatedReasoningStreamingModel(), [], renderer, "openai")

    assert msg.content == "final answer"
    assert renderer.text == ["final answer"]
    assert renderer.thinking == ["622"]


@pytest.mark.asyncio
async def test_stream_llm_drains_final_stream_events_before_return():
    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    ui_events.start(DockEventConsumer(test_dock))
    try:
        msg = await _stream_llm(
            FakeStreamingModel(),
            [],
            StreamingRenderer(Console(), debug=False),
            "anthropic",
            ui_port=make_presentation_ui(),
        )

        assert msg.content == "answer"
        rendered = "\n".join(_plain(line) for line in test_dock.tree.render(100))
        assert "answer" in rendered
        assert "Thinking" not in rendered
        assert "think" not in rendered
    finally:
        await ui_events.stop()
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)


@pytest.mark.asyncio
async def test_stream_llm_sanitizes_replayed_thinking_blocks():
    renderer = FakeRenderer()
    model = FakeStreamingModel()

    await _stream_llm(
        model,
        [
            HumanMessage(content="hi"),
            AIMessage(content=[
                {"type": "thinking", "text": "old thought"},
                {"type": "text", "text": "old answer"},
            ]),
        ],
        renderer,
        "anthropic",
    )

    assert model.messages[1].content == "old answer"


@pytest.mark.asyncio
async def test_stream_llm_repairs_missing_tool_results_before_replay():
    renderer = FakeRenderer()
    model = FakeStreamingModel()

    await _stream_llm(
        model,
        [
            HumanMessage(content="hi"),
            AIMessage(
                content=[
                    {"type": "tool_use", "id": "call_missing", "name": "read", "input": {}},
                ],
                tool_calls=[{"name": "read", "args": {}, "id": "call_missing", "type": "tool_call"}],
            ),
            HumanMessage(content="next"),
        ],
        renderer,
        "anthropic",
    )

    assert isinstance(model.messages[2], ToolMessage)
    assert model.messages[2].tool_call_id == "call_missing"
    assert model.messages[2].status == "error"
    assert model.messages[2].additional_kwargs["voidx_tool_observation"] == {
        "source": "replay_repair",
        "executed": False,
        "synthetic": True,
        "status": "error",
        "fallback_eligible": False,
    }
    assert model.messages[3].content == "next"


@pytest.mark.asyncio
async def test_stream_llm_repairs_missing_tool_results_from_additional_kwargs():
    renderer = FakeRenderer()
    model = FakeStreamingModel()

    await _stream_llm(
        model,
        [
            HumanMessage(content="hi"),
            AIMessage(
                content="",
                additional_kwargs={
                    "tool_calls": [
                        {"id": "call_raw", "function": {"name": "read", "arguments": "{}"}},
                    ],
                },
            ),
            HumanMessage(content="next"),
        ],
        renderer,
        "openai",
    )

    assert isinstance(model.messages[2], ToolMessage)
    assert model.messages[2].tool_call_id == "call_raw"
    assert model.messages[2].status == "error"
    assert model.messages[3].content == "next"


@pytest.mark.asyncio
async def test_stream_llm_sanitizes_replayed_failed_tool_exchanges():
    renderer = FakeRenderer()
    model = FakeStreamingModel()

    await _stream_llm(
        model,
        [
            HumanMessage(content="hi"),
            AIMessage(
                content=[
                    {"type": "tool_use", "id": "call_error", "name": "read", "input": {}},
                    {"type": "tool_use", "id": "call_ok", "name": "search", "input": {}},
                ],
                tool_calls=[
                    {"name": "read", "args": {}, "id": "call_error", "type": "tool_call"},
                    {"name": "search", "args": {}, "id": "call_ok", "type": "tool_call"},
                ],
                additional_kwargs={
                    "tool_calls": [
                        {"id": "call_error", "function": {"name": "read", "arguments": "{}"}},
                        {"id": "call_ok", "function": {"name": "search", "arguments": "{}"}},
                    ]
                },
            ),
            ToolMessage(content="failed", tool_call_id="call_error", status="error"),
            ToolMessage(content="ok", tool_call_id="call_ok"),
            AIMessage(content="I will recover from the failed read."),
            HumanMessage(content="next"),
        ],
        renderer,
        "anthropic",
    )

    replay_ai = model.messages[1]
    assert isinstance(replay_ai, AIMessage)
    assert [call["id"] for call in replay_ai.tool_calls] == ["call_error", "call_ok"]
    assert replay_ai.content == [
        {"type": "tool_use", "id": "call_error", "name": "read", "input": {}},
        {"type": "tool_use", "id": "call_ok", "name": "search", "input": {}},
    ]
    assert replay_ai.additional_kwargs["tool_calls"] == [
        {"id": "call_error", "function": {"name": "read", "arguments": "{}"}},
        {"id": "call_ok", "function": {"name": "search", "arguments": "{}"}},
    ]
    assert isinstance(model.messages[2], ToolMessage)
    assert model.messages[2].tool_call_id == "call_error"
    assert model.messages[2].content == "failed"
    assert model.messages[2].status == "error"
    assert isinstance(model.messages[3], ToolMessage)
    assert model.messages[3].tool_call_id == "call_ok"
    assert model.messages[4].content == "I will recover from the failed read."
    assert model.messages[5].content == "next"


@pytest.mark.asyncio
async def test_stream_llm_preserves_todo_tool_call_not_in_trailing_segment():
    renderer = FakeRenderer()
    model = FakeStreamingModel()

    await _stream_llm(
        model,
        [
            HumanMessage(content="hi"),
            AIMessage(
                content="",
                tool_calls=[{"name": "todo", "args": {}, "id": "call_todo", "type": "tool_call"}],
            ),
            HumanMessage(content="next"),
        ],
        renderer,
        "anthropic",
    )

    assert [type(message) for message in model.messages] == [HumanMessage, AIMessage, ToolMessage, HumanMessage]
    assert model.messages[1].tool_calls[0]["id"] == "call_todo"
    assert model.messages[2].tool_call_id == "call_todo"
    assert model.messages[3].content == "next"


@pytest.mark.asyncio
async def test_stream_llm_preserves_compact_tool_calls_when_not_sanitized():
    renderer = FakeRenderer()
    model = FakeStreamingModel()

    await _stream_llm(
        model,
        [
            HumanMessage(content="hi"),
            AIMessage(
                content="",
                tool_calls=[{"name": "compact", "args": {}, "id": "call_runtime", "type": "tool_call"}],
            ),
            ToolMessage(content="runtime output", tool_call_id="call_runtime"),
            HumanMessage(content="next"),
        ],
        renderer,
        "anthropic",
    )

    # Tool results remain available to the LLM during replay.
    assert [type(message) for message in model.messages] == [HumanMessage, AIMessage, ToolMessage, HumanMessage]


@pytest.mark.asyncio
async def test_stream_llm_sanitizes_replayed_workflow_tool_calls():
    renderer = FakeRenderer()
    model = FakeStreamingModel()

    await _stream_llm(
        model,
        [
            HumanMessage(content="hi"),
            AIMessage(
                content="",
                tool_calls=[{"name": "workflow", "args": {}, "id": "call_workflow", "type": "tool_call"}],
            ),
            ToolMessage(content="workflow output", tool_call_id="call_workflow"),
            HumanMessage(content="next"),
        ],
        renderer,
        "anthropic",
    )

    assert [type(message) for message in model.messages] == [HumanMessage, AIMessage, ToolMessage, HumanMessage]
    assert [message.content for message in model.messages] == ["hi", "", "workflow output", "next"]


@pytest.mark.parametrize("tool_name", ["checkpoint", "clarify"])
@pytest.mark.asyncio
async def test_stream_llm_preserves_user_decision_tool_calls_for_replay(tool_name):
    renderer = FakeRenderer()
    model = FakeStreamingModel()

    await _stream_llm(
        model,
        [
            HumanMessage(content="hi"),
            AIMessage(
                content="",
                tool_calls=[{"name": tool_name, "args": {}, "id": "call_decision", "type": "tool_call"}],
            ),
            ToolMessage(content="user decision", tool_call_id="call_decision"),
            HumanMessage(content="next"),
        ],
        renderer,
        "anthropic",
    )

    assert [type(message) for message in model.messages] == [
        HumanMessage,
        AIMessage,
        ToolMessage,
        HumanMessage,
    ]
    assert model.messages[1].tool_calls[0]["name"] == tool_name
    assert model.messages[2].content == "user decision"


@pytest.mark.asyncio
async def test_stream_llm_preserves_current_todo_tool_result():
    renderer = FakeRenderer()
    model = FakeStreamingModel()

    await _stream_llm(
        model,
        [
            HumanMessage(content="hi"),
            AIMessage(
                content="",
                tool_calls=[{"name": "todo", "args": {}, "id": "call_todo", "type": "tool_call"}],
            ),
            ToolMessage(content="todo output", tool_call_id="call_todo"),
        ],
        renderer,
        "anthropic",
    )

    assert [type(message) for message in model.messages] == [HumanMessage, AIMessage, ToolMessage]
    assert model.messages[1].tool_calls[0]["id"] == "call_todo"
    assert model.messages[2].tool_call_id == "call_todo"


@pytest.mark.asyncio
async def test_stream_llm_preserves_non_todo_call_in_mixed_batch():
    renderer = FakeRenderer()
    model = FakeStreamingModel()

    await _stream_llm(
        model,
        [
            HumanMessage(content="hi"),
            AIMessage(
                content=[
                    {"type": "tool_use", "id": "call_todo", "name": "todo", "input": {}},
                    {"type": "tool_use", "id": "call_read", "name": "read", "input": {}},
                ],
                tool_calls=[
                    {"name": "todo", "args": {}, "id": "call_todo", "type": "tool_call"},
                    {"name": "read", "args": {}, "id": "call_read", "type": "tool_call"},
                ],
            ),
            ToolMessage(content="read output", tool_call_id="call_read"),
            HumanMessage(content="next"),
        ],
        renderer,
        "anthropic",
    )

    replay_ai = model.messages[1]
    assert isinstance(replay_ai, AIMessage)
    assert [call["id"] for call in replay_ai.tool_calls] == ["call_todo", "call_read"]
    assert replay_ai.content == [
        {"type": "tool_use", "id": "call_todo", "name": "todo", "input": {}},
        {"type": "tool_use", "id": "call_read", "name": "read", "input": {}},
    ]
    tool_message_ids = {
        m.tool_call_id for m in model.messages if isinstance(m, ToolMessage)
    }
    assert tool_message_ids == {"call_todo", "call_read"}


@pytest.mark.asyncio
async def test_stream_llm_parses_dsml_text_tool_calls():
    renderer = FakeRenderer()

    msg = await _stream_llm(FakeDsmlStreamingModel(), [], renderer, "anthropic")

    assert msg.content == ""
    assert msg.tool_calls == [
        {
            "name": "search",
            "args": {
                "path": "src/voidx/presentation/commands.py",
                "pattern": "permissions",
            },
            "id": msg.tool_calls[0]["id"],
            "type": "tool_call",
        }
    ]
    assert msg.tool_calls[0]["id"].startswith("call_dsml_")
    assert renderer.text == []


@pytest.mark.asyncio
async def test_stream_llm_ignores_malformed_dsml_pipe_runs():
    renderer = FakeRenderer()

    msg = await _stream_llm(FakeMalformedDsmlStreamingModel(), [], renderer, "anthropic")

    assert msg.tool_calls == []
    assert msg.content == ""
    assert msg.response_metadata["malformed_tool_call"] is True
    assert msg.response_metadata["malformed_tool_call_format"] == "dsml"
    assert renderer.text == []


@pytest.mark.asyncio
async def test_stream_llm_marks_malformed_legacy_xml_tool_call():
    renderer = FakeRenderer()

    msg = await _stream_llm(FakeMalformedLegacyXmlStreamingModel(), [], renderer, "anthropic")

    assert msg.tool_calls == []
    assert msg.content == ""
    assert msg.response_metadata["malformed_tool_call"] is True
    assert msg.response_metadata["malformed_tool_call_format"] == "legacy_xml"
    assert renderer.text == []


@pytest.mark.asyncio
async def test_stream_llm_marks_malformed_provider_json_tool_call():
    renderer = FakeRenderer()

    msg = await _stream_llm(FakeMalformedProviderJsonToolCallStreamingModel(), [], renderer, "openai")

    assert msg.tool_calls == []
    assert msg.content == ""
    assert msg.response_metadata["malformed_tool_call"] is True
    assert msg.response_metadata["malformed_tool_call_format"] == "provider_json"
    assert renderer.text == []


@pytest.mark.asyncio
async def test_stream_llm_parses_legacy_xml_text_tool_calls():
    renderer = FakeRenderer()

    msg = await _stream_llm(FakeLegacyXmlToolCallStreamingModel(), [], renderer, "anthropic")

    assert msg.content == ""
    assert msg.tool_calls == [
        {
            "name": "read",
            "args": {
                "file_path": "src/voidx/permission/engine.py",
                "offset": 110,
                "limit": 50,
            },
            "id": msg.tool_calls[0]["id"],
            "type": "tool_call",
        }
    ]
    assert msg.tool_calls[0]["id"].startswith("call_xml_")
    assert renderer.text == []


@pytest.mark.asyncio
async def test_stream_llm_parses_legacy_xml_arg_pair_tool_name():
    renderer = FakeRenderer()

    msg = await _stream_llm(FakeLegacyXmlArgPairToolCallStreamingModel(), [], renderer, "anthropic")

    assert msg.content == ""
    assert msg.tool_calls[0]["name"] == "read"
    assert msg.tool_calls[0]["args"] == {
        "file_path": "src/voidx/permission/engine.py",
        "offset": 110,
    }
    assert renderer.text == []


@pytest.mark.asyncio
async def test_stream_llm_strips_legacy_dsml_blocks_before_replay():
    renderer = FakeRenderer()
    model = FakeStreamingModel()

    await _stream_llm(
        model,
        [
            HumanMessage(content="hi"),
            AIMessage(content=(
                '也必须在 commands 列表中注册:\n\n'
                '<｜｜DSML｜｜tool_calls>\n'
                '<｜｜DSML｜｜invoke name="search">\n'
                '<｜｜DSML｜｜parameter name="path" string="true">src/voidx/presentation/commands.py</｜｜DSML｜｜parameter>\n'
                '</｜｜DSML｜｜invoke>\n'
                '</｜｜DSML｜｜tool_calls>'
            )),
        ],
        renderer,
        "anthropic",
    )

    assert len(model.messages) == 1
    assert model.messages[0].content == "hi"


@pytest.mark.asyncio
async def test_stream_llm_preserves_usage_metadata():
    renderer = FakeRenderer()

    msg = await _stream_llm(FakeUsageStreamingModel(), [], renderer, "openai")

    assert msg.usage_metadata == {
        "input_tokens": 7,
        "output_tokens": 3,
        "total_tokens": 10,
    }


@pytest.mark.asyncio
async def test_stream_llm_reports_start_and_each_stream_chunk_activity():
    renderer = FakeRenderer()
    observed: list[str] = []

    await _stream_llm(
        FakeStreamingModel(),
        [],
        renderer,
        "anthropic",
        on_activity=lambda: observed.append("activity"),
    )

    assert observed == ["activity", "activity", "activity", "activity"]


@pytest.mark.asyncio
async def test_stream_llm_discards_partial_stream_before_done_on_failure():
    class FailingStreamingModel:
        async def astream(self, _messages):
            yield AIMessageChunk(content="partial answer")
            raise RuntimeError("mid-stream failure")

    class RecordingRenderer:
        def __init__(self):
            self.events: list[str] = []
            self.discarded = False

        def start(self):
            self.events.append("start")

        def feed_text(self, text: str):
            self.events.append(f"text:{text}")

        def feed_thinking(self, text: str):
            self.events.append(f"thinking:{text}")

        def discard(self):
            self.discarded = True
            self.events.append("discard")

        def done(self):
            self.events.append("done")
            if not self.discarded:
                self.events.append("commit")

    renderer = RecordingRenderer()

    with pytest.raises(RuntimeError, match="mid-stream failure"):
        await _stream_llm(FailingStreamingModel(), [], renderer, "openai")

    assert renderer.events == [
        "start",
        "text:partial answer",
        "discard",
        "done",
    ]
