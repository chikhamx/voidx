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


class NaturalLanguageWithToolCallKeywordsStreamingModel:
    def bind_tools(self, tool_defs):
        return self

    async def astream(self, messages):
        yield AIMessageChunk(content=(
            "分析异常原因如下：\n"
            "在处理 tool_calls 时，代码会检查 function 和 arguments：\n"
            '{"name": "read", "arguments": {"file_path": "test.py"}}'
        ))


class MultilineMalformedProviderJsonStreamingModel:
    def bind_tools(self, tool_defs):
        return self

    async def astream(self, messages):
        yield AIMessageChunk(content=(
            "{\n"
            '  "tool_calls": [\n'
            '    {"function": {"name": "read", "arguments": "{\\"file_path\\": \\"test.py\\"}"}}\n'
        ))


@pytest.mark.asyncio
async def test_stream_llm_does_not_mark_natural_language_as_malformed_tool_call():
    renderer = FakeRenderer()

    msg = await _stream_llm(NaturalLanguageWithToolCallKeywordsStreamingModel(), [], renderer, "openai")

    assert msg.tool_calls == []
    assert "分析异常原因如下：" in msg.content
    assert msg.response_metadata.get("malformed_tool_call") is not True
    assert renderer.text != []


@pytest.mark.asyncio
async def test_stream_llm_marks_multiline_malformed_provider_json_tool_call():
    renderer = FakeRenderer()

    msg = await _stream_llm(MultilineMalformedProviderJsonStreamingModel(), [], renderer, "openai")

    assert msg.tool_calls == []
    assert msg.content == ""
    assert msg.response_metadata["malformed_tool_call"] is True
    assert msg.response_metadata["malformed_tool_call_format"] == "provider_json"
    assert renderer.text == []
class NaturalLanguageWithXmlOrDsmlKeywordsStreamingModel:
    def bind_tools(self, tool_defs):
        return self

    async def astream(self, messages):
        yield AIMessageChunk(content=(
            "代码审查分析如下：\n"
            "系统对旧版的 <tool_call> 和 DSML (<||DSML||invoke) 协议进行了兼容处理。\n"
            "这是正常的自然语言解释。"
        ))


@pytest.mark.asyncio
async def test_stream_llm_does_not_mark_natural_language_mentioning_xml_or_dsml_as_malformed():
    renderer = FakeRenderer()

    msg = await _stream_llm(NaturalLanguageWithXmlOrDsmlKeywordsStreamingModel(), [], renderer, "openai")

    assert msg.tool_calls == []
    assert "代码审查分析如下：" in msg.content
    assert msg.response_metadata.get("malformed_tool_call") is not True
    assert renderer.text != []
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

    assert observed == ["activity", "activity", "activity"]


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


class SanitizationChunkModel:
    def __init__(self, chunks):
        self.chunks = chunks

    async def astream(self, messages):
        for chunk in self.chunks:
            yield chunk


@pytest.mark.parametrize("protocol", ["gemini", "anthropic"])
@pytest.mark.parametrize("thinking,text", [
    ('states["coding"] is the answer', 'states["coding'),
    ("a" * 40 + " thought", "a" * 40),
    ("a" * 40, "a" * 40 + " answer"),
    ("same", "same"),
])
@pytest.mark.asyncio
async def test_stream_llm_preserves_native_thinking_text(protocol, thinking, text):
    renderer = FakeRenderer()
    model = SanitizationChunkModel([
        AIMessageChunk(content=[
            {"type": "thinking", "thinking": thinking},
            {"type": "text", "text": text},
        ]),
        AIMessageChunk(content=[{"type": "text", "text": '"] remains'}]),
    ])
    msg = await _stream_llm(model, [], renderer, protocol)
    assert "".join(renderer.text) == text + '"] remains'
    assert msg.content == text + '"] remains'
    assert renderer.thinking == [thinking]


@pytest.mark.parametrize("protocol", ["openai", "deepseek", ""])
@pytest.mark.parametrize("thinking,text", [
    ("`thought", "`"),
    ("a" * 40 + " thought", "a" * 40),
    ("a" * 40, "a" * 40 + " answer"),
    (" ", "\n"),
])
def test_visible_content_preserves_nonidentical_prefixes(protocol, thinking, text):
    from voidx.agent.adapters.langgraph.runtime.streaming import _stream_visible_content

    for content in (text, [text], [{"type": "text", "text": text}]):
        assert _stream_visible_content(content, thinking, protocol=protocol) == content


@pytest.mark.parametrize("protocol", ["openai", "deepseek", ""])
@pytest.mark.parametrize("text", ["thought", " thought "])
def test_visible_content_keeps_exact_reasoning_deduplication(protocol, text):
    from voidx.agent.adapters.langgraph.runtime.streaming import _stream_visible_content

    assert _stream_visible_content(text, "thought", protocol=protocol) == ""


@pytest.mark.parametrize("text", ["", "  spaced text  ", " ", "\n", "\n    code\n"])
def test_tool_extractors_preserve_whitespace_without_calls(text):
    from voidx.agent.adapters.langgraph.runtime.streaming import (
        _extract_dsml_tool_calls_from_text,
        _extract_legacy_xml_tool_calls_from_text,
    )

    assert _extract_dsml_tool_calls_from_text(text) == (text, [])
    assert _extract_legacy_xml_tool_calls_from_text(text) == (text, [])


@pytest.mark.parametrize("parts", [
    ["TaskState() ", "runs. ", "So ", "host"],
    ["foo", " ", "bar"],
    ["\n", "    code", "\n"],
])
def test_replay_sanitization_preserves_whitespace(parts):
    from voidx.agent.adapters.langgraph.runtime.streaming import _sanitize_ai_content_for_replay

    expected = "".join(parts)
    for content in (expected, parts, [{"type": "text", "text": s} for s in parts]):
        assert _sanitize_ai_content_for_replay(content) == expected


@pytest.mark.asyncio
async def test_stream_llm_preserves_chunk_boundary_whitespace():
    renderer = FakeRenderer()
    model = SanitizationChunkModel([
        AIMessageChunk(content=[{"type": "text", "text": text}])
        for text in ["TaskState() ", "runs. ", "So ", "host"]
    ])
    msg = await _stream_llm(model, [], renderer, "gemini")
    assert "".join(renderer.text) == "TaskState() runs. So host"
    assert msg.content == "TaskState() runs. So host"


@pytest.mark.parametrize("protocol", ["deepseek", "gemini", "anthropic", "openai"])
def test_replay_sanitization_preserves_protocol_thinking_policy(protocol):
    from voidx.agent.adapters.langgraph.runtime.streaming import _sanitize_ai_content_for_replay

    thought = {"type": "reasoning_content", "text": "thought"}
    content = [thought, {"type": "text", "text": "answer"}]
    expected = content if protocol == "deepseek" else "answer"
    assert _sanitize_ai_content_for_replay(content, protocol=protocol) == expected


@pytest.mark.asyncio
async def test_stream_llm_keeps_invalid_dsml_protection():
    renderer = FakeRenderer()
    model = SanitizationChunkModel([AIMessageChunk(content=(
        '<||DSML||tool_calls><||DSML||invoke></||DSML||invoke></||DSML||tool_calls>'
    ))])
    msg = await _stream_llm(model, [], renderer, "gemini")
    assert msg.content == ""
    assert msg.tool_calls == []
    assert msg.response_metadata["malformed_tool_call"] is True
    assert renderer.text == []
