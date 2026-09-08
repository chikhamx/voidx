"""Regression tests for plain-text terminal turns without a stop call."""

import asyncio

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

from tests.langgraph_execution import make_langgraph_execution
from tests.test_agent.adapters.langgraph.runtime.stream_llm_helpers import FakeRenderer
from voidx.config import Config
from voidx.llm.domain.model import ModelConfig
from voidx.presentation.output.events import AssistantStreamCommitted, AssistantStreamUpdated


class ScriptedStreamingModel:
    def __init__(self, scripts: list[list[AIMessageChunk]]) -> None:
        self.scripts = list(scripts)
        self.call_index = 0
        self.bound_tools = None

    def bind_tools(self, tool_defs):
        self.bound_tools = tool_defs
        return self

    async def astream(self, messages):
        idx = self.call_index
        self.call_index += 1
        if idx >= len(self.scripts):
            pytest.fail(
                f"Unexpected LLM call {idx + 1}; "
                f"only {len(self.scripts)} scripted responses were provided"
            )
        for chunk in self.scripts[idx]:
            yield chunk


def _text_chunk(text: str) -> AIMessageChunk:
    return AIMessageChunk(content=text)


def _make_graph(tmp_path, model, monkeypatch, provider="openai"):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    async def fail_on_retry(delay):
        pytest.fail(f"Unexpected LLM retry with delay {delay}s")

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)
    monkeypatch.setattr(asyncio, "sleep", fail_on_retry)

    graph = make_langgraph_execution(
        Config(
            model=ModelConfig(provider=provider, model="test-model"),
            workspace=str(tmp_path),
        ),
        api_key=None,
    )
    graph.model = model
    return graph


@pytest.mark.asyncio
async def test_plain_text_in_running_state_without_pending_emits_text(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([[_text_chunk("Review completed: PASS")]])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="Run review")],
        "step_count": 1,
        "persona": "coordinate",
        "turn_state": "running",
    })

    msg = result["messages"][0]
    assert isinstance(msg, AIMessage)
    assert not msg.tool_calls
    assert msg.content == "Review completed: PASS"
    assert "LLM call failed" not in msg.content
    assert model.call_index == 1


@pytest.mark.asyncio
async def test_plain_text_emits_one_user_visible_committed_stream(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    emitted = []

    class TrackingEvents:
        async def emit(self, event):
            emitted.append(event)
            return True

        def emit_direct(self, event):
            emitted.append(event)
            return True

        async def drain(self):
            pass

        @property
        def is_running(self):
            return True

    class TrackingUi:
        class Output:
            def print(self, *args, **kwargs):
                pass

            def error(self, *args, **kwargs):
                pass

        def __init__(self):
            self.events = TrackingEvents()
            self.console = None
            self.ui = self.Output()

        def via_events(self):
            return True

    class TrackingRenderer(FakeRenderer):
        def feed_text(self, text: str) -> None:
            super().feed_text(text)

    model = ScriptedStreamingModel([
        [AIMessageChunk(content="")],
        [_text_chunk("Review completed: PASS")],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch, provider="gemini")
    graph._ui = TrackingUi()

    result = await graph._call_llm({
        "messages": [HumanMessage(content="Run review")],
        "step_count": 1,
        "persona": "coordinate",
        "turn_state": "initial",
    })

    assert result["messages"][0].content == "Review completed: PASS"
    assert model.call_index == 2
    stream_updates = [event for event in emitted if isinstance(event, AssistantStreamUpdated)]
    assert any("Review completed: PASS" in event.text for event in stream_updates)
    assert sum(isinstance(event, AssistantStreamCommitted) for event in emitted) == 1
