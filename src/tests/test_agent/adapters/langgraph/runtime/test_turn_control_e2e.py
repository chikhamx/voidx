"""End-to-end tests for turn_init and plain-text completion."""

import asyncio

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

from tests.langgraph_execution import make_langgraph_execution
from tests.test_agent.adapters.langgraph.runtime.stream_llm_helpers import FakeRenderer
from voidx.agent.adapters.langgraph.runtime.turn_control import TURN_TOOL_DEFINITION
from voidx.config import Config
from voidx.llm.domain.model import ModelConfig


class ScriptedStreamingModel:
    def __init__(self, scripts: list[list[AIMessageChunk]]) -> None:
        self.scripts = list(scripts)
        self.call_index = 0
        self.bound_tools = None

    def bind_tools(self, tool_defs):
        self.bound_tools = tool_defs
        return self

    async def astream(self, messages):
        index = self.call_index
        self.call_index += 1
        if index >= len(self.scripts):
            pytest.fail(
                f"Unexpected LLM call {index + 1}; "
                f"only {len(self.scripts)} scripted responses were provided"
            )
        for chunk in self.scripts[index]:
            yield chunk


def _turn_init_chunk(goal: str = "Fix the issue") -> AIMessageChunk:
    return AIMessageChunk(
        content="",
        tool_calls=[{
            "name": "turn_init",
            "args": {"goal": goal},
            "id": "tc-init",
            "type": "tool_call",
        }],
    )


def _legacy_turn_chunk() -> AIMessageChunk:
    return AIMessageChunk(
        content="",
        tool_calls=[{
            "name": "turn",
            "args": {"operation": "stop", "params": None},
            "id": "tc-legacy",
            "type": "tool_call",
        }],
    )


def _text_chunk(text: str) -> AIMessageChunk:
    return AIMessageChunk(content=text)


def _regular_tool_chunk() -> AIMessageChunk:
    return AIMessageChunk(
        content="",
        tool_calls=[{"name": "read", "args": {"file_path": "x.py"}, "id": "tc-read", "type": "tool_call"}],
    )


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
async def test_regular_tool_call_is_followed_by_plain_text_on_next_turn(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_regular_tool_chunk()],
        [_text_chunk("Final answer.")],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result1 = await graph._call_llm({
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
        "turn_state": "running",
    })
    assert result1["messages"][0].tool_calls[0]["name"] == "read"

    result2 = await graph._call_llm({
        "messages": [HumanMessage(content="hello"), *result1["messages"]],
        "step_count": 1,
        "persona": "coordinate",
        "turn_state": result1.get("turn_state", "running"),
    })
    assert result2["messages"][0].content == "Final answer."
    assert result2["messages"][0].tool_calls == []
    assert model.call_index == 2


@pytest.mark.asyncio
async def test_turn_init_never_emits_permission_or_execution_events(tmp_path, monkeypatch):
    events_emitted = []

    class TrackingEvents:
        async def emit(self, event):
            events_emitted.append(event)
            return True

        def emit_direct(self, event):
            events_emitted.append(event)
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

        def invalidate(self):
            pass

        def via_events(self):
            return True

    model = ScriptedStreamingModel([
        [_turn_init_chunk("Inspect the issue")],
        [_text_chunk("The answer.")],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)
    graph._ui = TrackingUi()

    await graph._call_llm({
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
        "turn_state": "initial",
    })

    event_types = [type(event).__name__ for event in events_emitted]
    assert "ToolPermissionRequested" not in event_types
    assert "ToolStarted" not in event_types
    assert "ToolFinished" not in event_types
    assert model.call_index == 2


@pytest.mark.asyncio
async def test_plain_text_emits_one_committed_terminal_message(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([[_text_chunk("The committed answer.")]])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
        "turn_state": "running",
    })

    assert len(result["messages"]) == 1
    assert isinstance(result["messages"][0], AIMessage)
    assert result["messages"][0].content == "The committed answer."
    assert result["messages"][0].tool_calls == []
    assert model.call_index == 1


@pytest.mark.asyncio
async def test_legacy_turn_without_text_is_not_executed(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_legacy_turn_chunk()],
        [_text_chunk("Recovered answer.")],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
        "turn_state": "running",
    })

    assert result["messages"][0].content == "Recovered answer."
    assert result["messages"][0].tool_calls == []
    assert model.call_index == 2


@pytest.mark.asyncio
async def test_subagent_tool_definitions_do_not_register_lifecycle_tool(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([[_text_chunk("answer")]])
    graph = _make_graph(tmp_path, model, monkeypatch)

    assert "turn" not in graph.tools.ids()
    assert "turn_init" not in graph.tools.ids()
    assert TURN_TOOL_DEFINITION["function"]["name"] == "turn_init"
    assert graph.tools.get("turn") is None
    assert graph.tools.get("turn_init") is None
