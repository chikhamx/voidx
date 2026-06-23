"""Tests for stream LLM sanitization, DSML, and replay."""

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
                    {"type": "tool_use", "id": "call_ok", "name": "grep", "input": {}},
                ],
                tool_calls=[
                    {"name": "read", "args": {}, "id": "call_error", "type": "tool_call"},
                    {"name": "grep", "args": {}, "id": "call_ok", "type": "tool_call"},
                ],
                additional_kwargs={
                    "tool_calls": [
                        {"id": "call_error", "function": {"name": "read", "arguments": "{}"}},
                        {"id": "call_ok", "function": {"name": "grep", "arguments": "{}"}},
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
    assert [call["id"] for call in replay_ai.tool_calls] == ["call_ok"]
    assert replay_ai.content == [
        {"type": "tool_use", "id": "call_ok", "name": "grep", "input": {}},
    ]
    assert replay_ai.additional_kwargs["tool_calls"] == [
        {"id": "call_ok", "function": {"name": "grep", "arguments": "{}"}},
    ]
    assert isinstance(model.messages[2], ToolMessage)
    assert model.messages[2].tool_call_id == "call_ok"
    assert model.messages[3].content == "I will recover from the failed read."
    assert model.messages[4].content == "next"
    assert not any(
        isinstance(message, ToolMessage) and message.tool_call_id == "call_error"
        for message in model.messages
    )


@pytest.mark.asyncio
async def test_stream_llm_strips_todo_tool_call_before_repair():
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

    assert [type(message) for message in model.messages] == [HumanMessage, HumanMessage]
    assert [message.content for message in model.messages] == ["hi", "next"]


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

    # compact is not in _REPLAY_SANITIZED_TOOL_NAMES, so its tool call and
    # ToolMessage are preserved for the LLM.
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

    assert [type(message) for message in model.messages] == [HumanMessage, HumanMessage]
    assert [message.content for message in model.messages] == ["hi", "next"]


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
    assert [call["id"] for call in replay_ai.tool_calls] == ["call_read"]
    assert replay_ai.content == [{"type": "tool_use", "id": "call_read", "name": "read", "input": {}}]
    assert isinstance(model.messages[2], ToolMessage)
    assert model.messages[2].tool_call_id == "call_read"
    assert not any(
        isinstance(message, ToolMessage) and message.tool_call_id == "call_todo"
        for message in model.messages
    )


@pytest.mark.asyncio
async def test_stream_llm_parses_dsml_text_tool_calls():
    renderer = FakeRenderer()

    msg = await _stream_llm(FakeDsmlStreamingModel(), [], renderer, "anthropic")

    assert msg.content == ""
    assert msg.tool_calls == [
        {
            "name": "grep",
            "args": {
                "path": "src/voidx/ui/commands.py",
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
                '<｜｜DSML｜｜invoke name="grep">\n'
                '<｜｜DSML｜｜parameter name="path" string="true">src/voidx/ui/commands.py</｜｜DSML｜｜parameter>\n'
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
