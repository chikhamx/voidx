"""Integration tests for turn control inside _call_llm."""

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

from voidx.agent.graph import VoidXGraph
from voidx.config import Config, ModelConfig
from tests.test_agent.graph.stream_llm_helpers import FakeRenderer


class ScriptedStreamingModel:
    """Returns scripted AIMessageChunks in sequence, one per astream call."""

    def __init__(self, scripts: list[list[AIMessageChunk]]) -> None:
        self.scripts = list(scripts)
        self.call_index = 0
        self.bound_tools = None
        self.received_messages: list = []

    def bind_tools(self, tool_defs):
        self.bound_tools = tool_defs
        return self

    async def astream(self, messages):
        self.received_messages.append(messages)
        idx = self.call_index
        self.call_index += 1
        if idx < len(self.scripts):
            for chunk in self.scripts[idx]:
                yield chunk
            return
        yield AIMessageChunk(content="")


def _turn_call_chunk() -> AIMessageChunk:
    return AIMessageChunk(
        content="",
        tool_calls=[{"name": "turn", "args": {"decision": "stop"}, "id": "tc1", "type": "tool_call"}],
    )



def _turn_call_with_decision_chunk(decision: str) -> AIMessageChunk:
    return AIMessageChunk(
        content="",
        tool_calls=[{"name": "turn", "args": {"decision": decision}, "id": f"tc-{decision}", "type": "tool_call"}],
    )


def _turn_stop_with_text_chunk(text: str = "unexpected text") -> AIMessageChunk:
    return AIMessageChunk(
        content=text,
        tool_calls=[{"name": "turn", "args": {"decision": "stop"}, "id": "tc-stop-text", "type": "tool_call"}],
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
            {"name": "turn", "args": {"decision": "stop"}, "id": "tc4", "type": "tool_call"},
        ],
    )


class RecordingRenderer(FakeRenderer):
    visible_text: list[str] = []
    headless_values: list[bool] = []

    @classmethod
    def reset(cls) -> None:
        cls.visible_text = []
        cls.headless_values = []

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.headless = bool(kwargs.get("headless", False))
        type(self).headless_values.append(self.headless)

    def feed_text(self, text: str) -> None:
        super().feed_text(text)
        if not self.headless:
            type(self).visible_text.append(text)


def _make_graph(tmp_path, model, monkeypatch, provider="openai", renderer_cls=FakeRenderer):
    import voidx.agent.graph.core.llm as graph_module
    monkeypatch.setattr(graph_module, "StreamingRenderer", renderer_cls)

    graph = VoidXGraph(
        Config(
            model=ModelConfig(provider=provider, model="test-model"),
            workspace=str(tmp_path),
        ),
        api_key="test-key",
    )
    graph.model = model
    return graph


# ── Test 1: valid turn commits latest provisional response ──────────────────


@pytest.mark.asyncio
async def test_valid_turn_commits_provisional_response(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_text_chunk("Here is the answer.")],
        [_turn_call_chunk()],
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
    assert msgs[0].content == "Here is the answer."
    assert not msgs[0].tool_calls


# ── Regression: text and turn may be returned in the same message ──────────


@pytest.mark.asyncio
async def test_turn_with_same_message_text_commits_immediately(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([[
        AIMessageChunk(
            content="你好！有什么可以帮你？",
            tool_calls=[
                {
                    "name": "turn",
                    "args": {"decision": "stop"},
                    "id": "tc-same-message",
                    "type": "tool_call",
                }
            ],
        ),
    ]])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="你好")],
        "step_count": 0,
        "persona": "coordinate",
    })

    msgs = result["messages"]
    assert len(msgs) == 1
    assert msgs[0].content == "你好！有什么可以帮你？"
    assert not msgs[0].tool_calls
    assert model.call_index == 1


# ── Test 2: turn without pending provisional text is rejected ───────────────


@pytest.mark.asyncio
async def test_turn_without_pending_text_falls_back(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_turn_call_chunk()],
        [_regular_tool_chunk()],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
    })

    msgs = result["messages"]
    assert len(msgs) == 1
    assert msgs[0].tool_calls
    assert msgs[0].tool_calls[0]["name"] == "read"
    assert msgs[0].content == ""
    assert model.call_index == 2


# ── Regression: repeated empty turn calls must terminate ────────────────────


@pytest.mark.asyncio
async def test_repeated_turn_without_text_stops_after_one_repair(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_turn_call_chunk()],
        [_turn_call_chunk()],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
    })

    assert result["should_continue"] is False
    assert "invalid turn control call" in result["messages"][0].content
    assert model.call_index == 2


# ── Test 3: regular tool call still routes to tool execution ────────────────


@pytest.mark.asyncio
async def test_regular_tool_call_routes_to_execute(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_regular_tool_chunk()],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="read x.py")],
        "step_count": 0,
        "persona": "coordinate",
    })

    msgs = result["messages"]
    assert len(msgs) == 1
    assert msgs[0].tool_calls
    assert msgs[0].tool_calls[0]["name"] == "read"


# ── Test 4: first plain-text triggers first completion prompt ───────────────


@pytest.mark.asyncio
async def test_first_plain_text_triggers_first_prompt_then_turn(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_text_chunk("Provisional answer.")],
        [_turn_call_chunk()],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
    })

    msgs = result["messages"]
    assert msgs[0].content == "Provisional answer."
    assert not msgs[0].tool_calls
    assert model.call_index == 2


@pytest.mark.asyncio
async def test_missing_turn_reprompt_keeps_full_tool_set_and_appends_turn_prompt(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_text_chunk("Provisional answer.")],
        [_turn_call_chunk()],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
    })

    assert result["messages"][0].content == "Provisional answer."
    assert model.call_index == 2
    turn_prompt_messages = model.received_messages[1]
    assert any(
        isinstance(msg, AIMessage) and msg.content == "Provisional answer."
        for msg in turn_prompt_messages
    )
    assert any(
        isinstance(msg, HumanMessage) and "regular tool" in str(msg.content).lower()
        for msg in turn_prompt_messages
    )
    tool_names = [tool["function"]["name"] for tool in model.bound_tools]
    assert "turn" in tool_names
    assert "read" in tool_names


@pytest.mark.asyncio
async def test_regular_tool_after_turn_prompt_continues_without_committing_text(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_text_chunk("Needs more work.")],
        [_regular_tool_chunk()],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="read x.py")],
        "step_count": 0,
        "persona": "coordinate",
    })

    assert result["messages"][0].tool_calls
    assert result["messages"][0].tool_calls[0]["name"] == "read"
    assert result["messages"][0].content == ""
    assert model.call_index == 2


@pytest.mark.asyncio
async def test_decision_only_turn_with_text_is_rejected_before_stop(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_text_chunk("Provisional answer.")],
        [_turn_stop_with_text_chunk("extra text")],
        [_turn_call_chunk()],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
    })

    assert result["messages"][0].content == "Provisional answer."
    assert model.call_index == 3


# ── Test 5: second consecutive plain-text exits without another prompt ──────


@pytest.mark.asyncio
async def test_plain_text_during_decision_prompt_is_rejected_without_ui_leak(tmp_path, monkeypatch):
    RecordingRenderer.reset()
    model = ScriptedStreamingModel([
        [_text_chunk("First provisional.")],
        [_text_chunk("Second provisional.")],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch, renderer_cls=RecordingRenderer)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
    })

    visible_text = "".join(RecordingRenderer.visible_text)
    assert result["should_continue"] is False
    assert "invalid turn control call" in result["messages"][0].content
    assert "First provisional." in visible_text
    assert "Second provisional." not in visible_text
    assert RecordingRenderer.headless_values[0] is False
    assert all(RecordingRenderer.headless_values[1:])


# ── Test 6: second plain-text fallback keeps latest response ────────────────


@pytest.mark.asyncio
async def test_invalid_turn_continue_recovers_with_regular_tool(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_text_chunk("First provisional.")],
        [_turn_call_with_decision_chunk("continue")],
        [_regular_tool_chunk()],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
    })

    assert result["messages"][0].tool_calls
    assert result["messages"][0].tool_calls[0]["name"] == "read"
    assert result["messages"][0].content == ""
    assert model.call_index == 3


# ── Test 7: mixed turn + regular tool is rejected ───────────────────────────


@pytest.mark.asyncio
async def test_mixed_turn_and_regular_rejected(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_mixed_chunk()],
        [_regular_tool_chunk()],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
    })

    assert result["messages"][0].tool_calls
    assert result["messages"][0].tool_calls[0]["name"] == "read"
    assert result["messages"][0].content == ""
    assert model.call_index == 2


# ── Test 8: turn tool definition is injected for openai protocol ────────────


@pytest.mark.asyncio
async def test_turn_tool_definition_injected_for_openai(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_text_chunk("answer")],
        [_turn_call_chunk()],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch, provider="openai")

    await graph._call_llm({
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
    })

    tool_names = [t["function"]["name"] for t in model.bound_tools]
    assert "turn" in tool_names


# ── Test 9: turn tool definition IS injected for deepseek protocol ──────────


@pytest.mark.asyncio
async def test_turn_tool_definition_injected_for_deepseek(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_text_chunk("answer"), _turn_call_chunk()],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch, provider="deepseek")

    result = await graph._call_llm({
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
    })

    tool_names = [t["function"]["name"] for t in model.bound_tools] if model.bound_tools else []
    assert "turn" in tool_names
    msgs = result["messages"]
    assert msgs[0].content == "answer"
    assert model.call_index == 1


# ── Regression: malformed turn arguments must be repaired ───────────────────


@pytest.mark.asyncio
async def test_turn_with_non_empty_args_is_rejected(tmp_path, monkeypatch):
    malformed_turn = AIMessageChunk(
        content="",
        tool_calls=[{
            "name": "turn",
            "args": {"unexpected": 1},
            "id": "tc-malformed",
            "type": "tool_call",
        }],
    )
    model = ScriptedStreamingModel([
        [_text_chunk("answer")],
        [malformed_turn],
        [_turn_call_chunk()],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
    })

    assert result["messages"][0].content == "answer"
    assert model.call_index == 3


# ── Regression: repeated mixed calls must never reach tool execution ────────


@pytest.mark.asyncio
async def test_repeated_mixed_turn_call_terminates_without_tools(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_mixed_chunk()],
        [_mixed_chunk()],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
    })

    assert result["should_continue"] is False
    assert not result["messages"][0].tool_calls
    assert graph._router({"messages": result["messages"]}) == "end"


# ── Regression: terminal normalization preserves provider metadata ──────────


def test_terminal_normalization_preserves_message_metadata():
    from voidx.agent.graph.turn_control import normalize_terminal_message

    pending = AIMessage(
        content="answer",
        id="message-1",
        name="assistant",
        response_metadata={"model": "test-model", "finish_reason": "stop"},
        usage_metadata={
            "input_tokens": 3,
            "output_tokens": 2,
            "total_tokens": 5,
        },
        additional_kwargs={"provider_field": "value", "tool_calls": ["internal"]},
    )

    normalized = normalize_terminal_message(pending)

    assert normalized.content == "answer"
    assert normalized.id == "message-1"
    assert normalized.name == "assistant"
    assert normalized.response_metadata == pending.response_metadata
    assert normalized.usage_metadata == pending.usage_metadata
    assert normalized.additional_kwargs == {"provider_field": "value"}
    assert not normalized.tool_calls
