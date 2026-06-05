import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage, ToolMessage

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from voidx.agent.graph.streaming import stream_llm as _stream_llm
from voidx.agent.graph import VoidXGraph
from voidx.config import Config, ModelConfig
from voidx.memory.context_frames import load_context_frames
from voidx.memory.session import MessageRow, create_session, delete_session, save_message


class FakeStreamingModel:
    def __init__(self) -> None:
        self.messages = None

    def bind_tools(self, tool_defs):
        return self

    async def astream(self, messages):
        self.messages = messages
        yield AIMessageChunk(content=[{"type": "thinking", "text": "think"}])
        yield AIMessageChunk(content="answer")


class FakeUsageStreamingModel:
    def bind_tools(self, tool_defs):
        return self

    async def astream(self, messages):
        yield AIMessageChunk(
            content="answer",
            usage_metadata={
                "input_tokens": 7,
                "output_tokens": 3,
                "total_tokens": 10,
            },
        )


class FakeDsmlStreamingModel:
    def __init__(self) -> None:
        self.messages = None

    def bind_tools(self, tool_defs):
        return self

    async def astream(self, messages):
        self.messages = messages
        yield AIMessageChunk(content=(
            '也必须在 commands 列表中注册:\n\n'
            '<｜｜DSML｜｜tool_calls>\n'
            '<｜｜DSML｜｜invoke name="grep">\n'
            '<｜｜DSML｜｜parameter name="path" string="true">src/voidx/ui/commands.py</｜｜DSML｜｜parameter>\n'
            '<｜｜DSML｜｜parameter name="pattern" string="true">permissions</｜｜DSML｜｜parameter>\n'
            '</｜｜DSML｜｜invoke>\n'
            '</｜｜DSML｜｜tool_calls>'
        ))


class FakeMalformedDsmlStreamingModel:
    def bind_tools(self, tool_defs):
        return self

    async def astream(self, messages):
        yield AIMessageChunk(content=(
            '<|||DSML||tool_calls>\n'
            '<||DSML||invoke name="grep">\n'
            '<||DSML||parameter name="path" string="true">src/voidx/ui/commands.py</||DSML||parameter>\n'
            '</||DSML||invoke>\n'
            '</|||DSML||tool_calls>'
        ))


class TrackingStreamingModel(FakeStreamingModel):
    def __init__(self) -> None:
        super().__init__()
        self.bound_tools = None

    def bind_tools(self, tool_defs):
        self.bound_tools = tool_defs
        return self


class FakeRenderer:
    def __init__(self, *args, **kwargs) -> None:
        self.text: list[str] = []
        self.thinking: list[str] = []
        self.started = False
        self.done_called = False
        self.discarded = False

    def start(self) -> None:
        self.started = True

    def feed_text(self, text: str) -> None:
        self.text.append(text)

    def feed_thinking(self, text: str) -> None:
        self.thinking.append(text)

    def discard(self) -> None:
        self.discarded = True

    def done(self) -> None:
        self.done_called = True


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
    assert model.messages[3].content == "next"


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
    assert "<|||DSML||tool_calls>" in msg.content


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


@pytest.mark.asyncio
async def test_call_llm_resolves_protocol_for_mimo_provider(tmp_path, monkeypatch):
    import voidx.agent.graph.core as graph_module

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
        "agent": "orchestrator",
    })

    assert result["step_count"] == 1
    assert result["messages"][0].content == "answer"


@pytest.mark.asyncio
async def test_call_llm_updates_usage_stats(tmp_path, monkeypatch):
    import voidx.agent.graph.core as graph_module

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
        "agent": "orchestrator",
    })

    assert result["step_count"] == 1
    assert graph._usage_stats.last_input_tokens == 7
    assert graph._usage_stats.last_output_tokens == 3
    assert graph._usage_stats.total_input_tokens == 7
    assert graph._usage_stats.total_output_tokens == 3
    assert graph._usage_stats.total_calls == 1


@pytest.mark.asyncio
async def test_call_llm_persists_context_frame_for_session(tmp_path, monkeypatch):
    import voidx.agent.graph.core as graph_module

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
                    "## Current Date\n2026-05-31 10:00 CST"
                )),
                HumanMessage(content="hi"),
            ],
            "step_count": 0,
            "max_steps": 1,
            "agent": "orchestrator",
            "user_message_id": user_message_id,
        })

        frames = await load_context_frames(session.id)
        assert len(frames) == 1
        assert frames[0].frame_kind == "main"
        assert frames[0].agent_role == "orchestrator"
        assert frames[0].user_message_id == user_message_id
        assert frames[0].messages[-1]["content"] == "hi"
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_call_llm_does_not_bind_tools_when_no_tool_step_budget(tmp_path, monkeypatch):
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
        "agent": "orchestrator",
    })

    assert result["messages"][0].content == "answer"
    assert model.bound_tools is None


@pytest.mark.asyncio
async def test_call_llm_filters_lsp_tools_when_no_lsp_server_is_available(tmp_path, monkeypatch):
    import voidx.agent.graph.core as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    graph = VoidXGraph(
        Config(
            model=ModelConfig(provider="openai", model="gpt-4o"),
            workspace=str(tmp_path),
        ),
        api_key=None,
    )
    graph._lsp_manager = SimpleNamespace(
        doctor=lambda: [SimpleNamespace(enabled=True, available=False)]
    )
    model = TrackingStreamingModel()
    graph.model = model

    await graph._call_llm({
        "messages": [HumanMessage(content="hi")],
        "step_count": 0,
        "max_steps": 50,
        "agent": "orchestrator",
    })

    tool_names = [tool["function"]["name"] for tool in model.bound_tools]
    assert tool_names
    assert not any(name.startswith("lsp_") for name in tool_names)


@pytest.mark.asyncio
async def test_call_llm_filters_tools_from_runtime_visible_tool_ids(tmp_path, monkeypatch):
    import voidx.agent.graph.core as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    graph = VoidXGraph(
        Config(
            model=ModelConfig(provider="openai", model="gpt-4o"),
            workspace=str(tmp_path),
        ),
        api_key=None,
    )
    model = TrackingStreamingModel()
    graph.model = model

    await graph._call_llm({
        "messages": [HumanMessage(content="hi")],
        "step_count": 0,
        "max_steps": 50,
        "agent": "orchestrator",
        "available_tool_ids": ["read", "grep"],
    })

    tool_names = [tool["function"]["name"] for tool in model.bound_tools]
    assert tool_names == ["read", "grep"]


@pytest.mark.asyncio
async def test_call_llm_keeps_lsp_tools_when_a_lsp_server_is_available(tmp_path, monkeypatch):
    import voidx.agent.graph.core as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    graph = VoidXGraph(
        Config(
            model=ModelConfig(provider="openai", model="gpt-4o"),
            workspace=str(tmp_path),
        ),
        api_key=None,
    )
    graph._lsp_manager = SimpleNamespace(
        doctor=lambda: [SimpleNamespace(enabled=True, available=True)]
    )
    model = TrackingStreamingModel()
    graph.model = model

    await graph._call_llm({
        "messages": [HumanMessage(content="hi")],
        "step_count": 0,
        "max_steps": 50,
        "agent": "orchestrator",
    })

    tool_names = [tool["function"]["name"] for tool in model.bound_tools]
    assert "lsp_diagnostics" in tool_names
    assert "lsp_symbols" in tool_names
