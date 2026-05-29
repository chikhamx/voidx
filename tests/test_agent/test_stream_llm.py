import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from voidx.agent.graph import _stream_llm
from voidx.agent.graph import VoidXGraph
from voidx.config import Config, ModelConfig


class FakeStreamingModel:
    def bind_tools(self, tool_defs):
        return self

    async def astream(self, messages):
        yield AIMessageChunk(content=[{"type": "thinking", "thinking": "think"}])
        yield AIMessageChunk(content="answer")


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

    assert msg.content == [{"type": "thinking", "thinking": "think"}, "answer"]
    assert renderer.started is True
    assert renderer.done_called is True
    assert renderer.discarded is False
    assert renderer.text == ["answer"]
    assert renderer.thinking == ["think"]


@pytest.mark.asyncio
async def test_call_llm_resolves_protocol_for_mimo_provider(tmp_path, monkeypatch):
    import voidx.agent.graph as graph_module

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
    assert result["messages"][0].content == [
        {"type": "thinking", "thinking": "think"},
        "answer",
    ]
