"""E2e tests for the 13 required test cases from the design doc."""

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

from voidx.agent.graph import VoidXGraph
from voidx.agent.graph.turn_control import TURN_TOOL_DEFINITION
from voidx.config import Config, ModelConfig
from tests.test_agent.graph.stream_llm_helpers import FakeRenderer


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


def _turn_chunk() -> AIMessageChunk:
    return AIMessageChunk(
        content="",
        tool_calls=[{"name": "turn", "args": {}, "id": "tc1", "type": "tool_call"}],
    )


def _text_chunk(text: str) -> AIMessageChunk:
    return AIMessageChunk(content=text)


def _regular_tool_chunk() -> AIMessageChunk:
    return AIMessageChunk(
        content="",
        tool_calls=[{"name": "read", "args": {"file_path": "x.py"}, "id": "tc2", "type": "tool_call"}],
    )


def _mixed_chunk() -> AIMessageChunk:
    return AIMessageChunk(
        content="",
        tool_calls=[
            {"name": "read", "args": {"file_path": "x.py"}, "id": "tc3", "type": "tool_call"},
            {"name": "turn", "args": {}, "id": "tc4", "type": "tool_call"},
        ],
    )


def _make_graph(tmp_path, model, monkeypatch, provider="openai"):
    import voidx.agent.graph.core.llm as graph_module

    async def fail_on_retry(delay):
        pytest.fail(f"Unexpected LLM retry with delay {delay}s")

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)
    monkeypatch.setattr(graph_module.asyncio, "sleep", fail_on_retry)

    graph = VoidXGraph(
        Config(
            model=ModelConfig(provider=provider, model="test-model"),
            workspace=str(tmp_path),
        ),
        api_key="test-key",
    )
    graph.model = model
    return graph


# ── Test 7: regular tool call resets missing-turn count ─────────────────────


@pytest.mark.asyncio
async def test_regular_tool_call_resets_missing_turn_count(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_text_chunk("First provisional.")],
        [_regular_tool_chunk()],
        [_text_chunk("Final answer.")],
        [_turn_chunk()],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)

    # First _call_llm: plain text → first miss prompt → regular tool call
    result1 = await graph._call_llm({
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
    })
    # The regular tool call should route to execute, not end
    assert result1["messages"][0].tool_calls
    assert result1["messages"][0].tool_calls[0]["name"] == "read"

    # Second _call_llm: fresh invocation, count should be reset
    # The model produces text then turn — should work in 2 calls
    result2 = await graph._call_llm({
        "messages": [HumanMessage(content="hello"), *result1["messages"]],
        "step_count": 1,
        "persona": "coordinate",
    })
    assert result2["messages"][0].content == "Final answer."
    assert not result2["messages"][0].tool_calls


# ── Test 10: control call never emits tool permission or execution events ───


@pytest.mark.asyncio
async def test_turn_call_never_emits_permission_or_execution_events(tmp_path, monkeypatch):
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

        def via_events(self):
            return True

    model = ScriptedStreamingModel([
        [_text_chunk("The answer.")],
        [_turn_chunk()],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)

    # Replace the UI port to track events
    graph._ui = TrackingUi()

    result = await graph._call_llm({
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
    })

    # Check that no tool permission or tool execution events were emitted
    event_types = [type(e).__name__ for e in events_emitted]
    assert "ToolPermissionRequested" not in event_types
    assert "ToolStarted" not in event_types
    assert "ToolFinished" not in event_types
    assert model.call_index == 2


# ── Test 11: successful barrier emits exactly one committed assistant stream ─


@pytest.mark.asyncio
async def test_successful_barrier_emits_one_committed_stream(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_text_chunk("The committed answer.")],
        [_turn_chunk()],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
    })

    msgs = result["messages"]
    # Exactly one AIMessage in the result
    assert len(msgs) == 1
    assert isinstance(msgs[0], AIMessage)
    assert msgs[0].content == "The committed answer."
    # No tool calls in the terminal message
    assert not msgs[0].tool_calls


# ── Test 12: second-miss fallback emits exactly one committed assistant stream ─


@pytest.mark.asyncio
async def test_second_miss_fallback_emits_one_committed_stream(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_text_chunk("First.")],
        [_text_chunk("Second — the committed one.")],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
    })

    msgs = result["messages"]
    assert len(msgs) == 1
    assert isinstance(msgs[0], AIMessage)
    assert msgs[0].content == "Second — the committed one."
    assert not msgs[0].tool_calls


# ── Test 13: subagent tool definitions do not contain turn ───────────────────


@pytest.mark.asyncio
async def test_subagent_tool_definitions_do_not_contain_turn(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_text_chunk("answer")],
        [_turn_chunk()],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)

    # The subagent path uses its own tools_for_llm() call
    # Verify that the turn tool is not in the subagent's tool definitions
    # by checking that the subagent's tool registry doesn't have it
    subagent_tool_ids = graph.tools.ids()
    assert "turn" not in subagent_tool_ids

    # Also verify the TURN_TOOL_DEFINITION is not registered as a normal tool
    assert TURN_TOOL_DEFINITION["function"]["name"] == "turn"
    # The tool registry should not contain a tool named "turn"
    assert graph.tools.get("turn") is None
