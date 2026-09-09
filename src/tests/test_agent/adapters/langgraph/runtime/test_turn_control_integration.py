"""Integration tests for turn_init and plain-text turn completion."""

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

from tests.langgraph_execution import make_langgraph_execution
from tests.test_agent.adapters.langgraph.runtime.stream_llm_helpers import FakeRenderer
from voidx.agent.adapters.langgraph.runtime.turn_control import TURN_TOOL_DEFINITION
from voidx.config import Config
from voidx.llm.domain.model import ModelConfig


class ScriptedStreamingModel:
    """Return one scripted response per astream call."""

    def __init__(self, scripts: list[list[AIMessageChunk]]) -> None:
        self.scripts = list(scripts)
        self.call_index = 0
        self.bound_tools = None
        self.received_messages: list[list] = []

    def bind_tools(self, tool_defs):
        self.bound_tools = tool_defs
        return self

    async def astream(self, messages):
        self.received_messages.append(list(messages))
        index = self.call_index
        self.call_index += 1
        if index < len(self.scripts):
            for chunk in self.scripts[index]:
                yield chunk
            return
        yield AIMessageChunk(content="")


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


def _turn_init_with_args_chunk(args: dict) -> AIMessageChunk:
    return AIMessageChunk(
        content="",
        tool_calls=[{
            "name": "turn_init",
            "args": args,
            "id": "tc-init-invalid",
            "type": "tool_call",
        }],
    )


def _legacy_turn_chunk(content: str = "", args: dict | None = None) -> AIMessageChunk:
    return AIMessageChunk(
        content=content,
        tool_calls=[{
            "name": "turn",
            "args": args if args is not None else {"operation": "stop", "params": None},
            "id": "tc-legacy",
            "type": "tool_call",
        }],
    )


def _legacy_mixed_chunk() -> AIMessageChunk:
    return AIMessageChunk(
        content="",
        tool_calls=[
            {"name": "read", "args": {"file_path": "x.py"}, "id": "tc-read", "type": "tool_call"},
            {"name": "turn", "args": {"operation": "stop", "params": None}, "id": "tc-legacy", "type": "tool_call"},
        ],
    )


def _turn_init_with_regular_tools_chunk(goal: str = "Inspect file") -> AIMessageChunk:
    return AIMessageChunk(
        content="",
        tool_calls=[
            {
                "name": "turn_init",
                "args": {"goal": goal},
                "id": "tc-init-mixed",
                "type": "tool_call",
            },
            {"name": "read", "args": {"file_path": "x.py"}, "id": "tc-read", "type": "tool_call"},
        ],
    )


def _regular_tool_chunk() -> AIMessageChunk:
    return AIMessageChunk(
        content="",
        tool_calls=[{"name": "read", "args": {"file_path": "x.py"}, "id": "tc-read", "type": "tool_call"}],
    )


def _text_chunk(text: str) -> AIMessageChunk:
    return AIMessageChunk(content=text)


def _make_graph(tmp_path, model, monkeypatch, provider="openai", renderer_cls=FakeRenderer):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", renderer_cls)
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
async def test_turn_init_accepts_goal_then_continues_to_plain_text(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_turn_init_chunk(goal="Implement turn init")],
        [_text_chunk("Done.")],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="start work")],
        "step_count": 0,
        "persona": "coordinate",
        "turn_state": "initial",
    })

    assert result["messages"][0].content == "Done."
    assert result["turn_state"] == "committed"
    assert result["task_state"]["current_goal"] == {"desc": "Implement turn init"}
    assert model.call_index == 2
    assert any(
        isinstance(message, ToolMessage)
        and message.name == "turn_init"
        and "Turn initialized" in str(message.content)
        for message in model.received_messages[1]
    )


@pytest.mark.asyncio
async def test_turn_init_with_regular_tools_initializes_then_routes_tools(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([[_turn_init_with_regular_tools_chunk("Inspect x.py")]])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="start work")],
        "step_count": 0,
        "persona": "coordinate",
        "turn_state": "initial",
    })

    assert result["turn_state"] == "running"
    assert result["task_state"]["current_goal"] == {"desc": "Inspect x.py"}
    assert [call["name"] for call in result["messages"][0].tool_calls] == ["read"]
    assert model.call_index == 1


@pytest.mark.asyncio
async def test_turn_init_with_regular_tools_does_not_replace_running_goal(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([[_turn_init_with_regular_tools_chunk("Should not replace")]])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="continue")],
        "step_count": 1,
        "persona": "coordinate",
        "turn_state": "running",
        "task_state": {"current_goal": {"desc": "Existing goal"}},
    })

    assert result["turn_state"] == "running"
    assert result["task_state"]["current_goal"] == {"desc": "Existing goal"}
    assert [call["name"] for call in result["messages"][0].tool_calls] == ["read"]
    assert model.call_index == 1


@pytest.mark.asyncio
async def test_plain_text_is_the_terminal_completion_without_lifecycle_call(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([[_text_chunk("Final answer.")]])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
        "turn_state": "running",
    })

    assert result["turn_state"] == "committed"
    assert result["messages"][0].content == "Final answer."
    assert result["messages"][0].tool_calls == []
    assert model.call_index == 1


@pytest.mark.asyncio
async def test_text_with_legacy_turn_is_normalized_to_terminal_text(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([[_legacy_turn_chunk("Final answer.")]])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
        "turn_state": "running",
    })

    assert result["turn_state"] == "committed"
    assert result["messages"][0].content == "Final answer."
    assert result["messages"][0].tool_calls == []
    assert model.call_index == 1


@pytest.mark.asyncio
async def test_legacy_turn_without_text_is_repaired_then_regular_tool_continues(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_legacy_turn_chunk()],
        [_regular_tool_chunk()],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="read x.py")],
        "step_count": 0,
        "persona": "coordinate",
        "turn_state": "running",
    })

    assert result["turn_state"] == "running"
    assert [call["name"] for call in result["messages"][0].tool_calls] == ["read"]
    assert model.call_index == 2


@pytest.mark.asyncio
async def test_repeated_legacy_turn_calls_fail_without_executing_tools(tmp_path, monkeypatch):
    from voidx.agent.domain.ui_events import ErrorAppended

    model = ScriptedStreamingModel([
        [_legacy_turn_chunk()],
        [_legacy_turn_chunk()],
        [_legacy_turn_chunk()],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)
    captured_events = []
    original_emit = graph._ui.events.emit

    async def mock_emit(event):
        captured_events.append(event)
        return await original_emit(event)

    monkeypatch.setattr(graph._ui.events, "emit", mock_emit)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
    })

    assert result["should_continue"] is False
    assert "invalid turn control call" in result["messages"][0].content
    assert not result["messages"][0].tool_calls
    assert model.call_index == 3
    assert any(isinstance(e, ErrorAppended) and "invalid turn control call" in e.message for e in captured_events)


@pytest.mark.asyncio
async def test_turn_init_accepts_extra_args_without_repair(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_turn_init_with_args_chunk({"goal": "Fix it", "extra": True})],
        [_text_chunk("Done.")],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
    })

    assert result["turn_state"] == "committed"
    assert result["messages"][0].content == "Done."
    assert graph._task_state.current_goal is not None
    assert graph._task_state.current_goal.desc == "Fix it"
    assert model.call_index == 2


@pytest.mark.asyncio
async def test_turn_init_invalid_args_are_repaired(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_turn_init_with_args_chunk({"goal": ""})],
        [_text_chunk("Done after repair.")],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
    })

    assert result["turn_state"] == "committed"
    assert result["messages"][0].content == "Done after repair."
    assert model.call_index == 2


@pytest.mark.asyncio
async def test_mixed_legacy_turn_is_repaired_without_tool_execution(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_legacy_mixed_chunk()],
        [_regular_tool_chunk()],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
    })

    assert [call["name"] for call in result["messages"][0].tool_calls] == ["read"]
    assert model.call_index == 2


@pytest.mark.asyncio
async def test_regular_tool_call_routes_to_execute(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([[_regular_tool_chunk()]])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="read x.py")],
        "step_count": 0,
        "persona": "coordinate",
    })

    assert result["messages"][0].tool_calls[0]["name"] == "read"
    assert model.call_index == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "deepseek"])
async def test_turn_init_tool_is_injected_for_model_protocol(tmp_path, monkeypatch, provider):
    model = ScriptedStreamingModel([[_text_chunk("answer")]])
    graph = _make_graph(tmp_path, model, monkeypatch, provider=provider)

    await graph._call_llm({
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
    })

    tool_names = [tool["function"]["name"] for tool in model.bound_tools]
    assert "turn_init" in tool_names
    assert "turn" not in tool_names


@pytest.mark.asyncio
async def test_turn_init_rerenders_context_with_running_state(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_turn_init_chunk(goal="Implement turn init")],
        [_text_chunk("Done.")],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)
    state = {
        "messages": [HumanMessage(content="start work")],
        "step_count": 0,
        "persona": "coordinate",
        "turn_state": "initial",
    }

    await graph._prepare_with_stream(state)
    await graph._call_llm(state)

    second_round_text = "\n".join(str(message.content) for message in model.received_messages[1])
    assert "Turn state: running" in second_round_text
    assert "Implement turn init" in second_round_text


@pytest.mark.asyncio
async def test_initial_empty_response_retries_then_accepts_turn_init_and_text(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [AIMessageChunk(content="")],
        [_turn_init_chunk(goal="Continue the task")],
        [_text_chunk("Done after retry.")],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="continue")],
        "step_count": 0,
        "persona": "coordinate",
        "turn_state": "initial",
    })

    assert result["turn_state"] == "committed"
    assert result["task_state"]["current_goal"] == {"desc": "Continue the task"}
    assert result["messages"][0].content == "Done after retry."
    assert model.call_index == 3


@pytest.mark.asyncio
async def test_running_empty_response_retries_then_accepts_regular_tool(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [AIMessageChunk(content="")],
        [_regular_tool_chunk()],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="read x.py")],
        "step_count": 0,
        "persona": "coordinate",
        "turn_state": "running",
    })

    assert result["messages"][0].tool_calls[0]["name"] == "read"
    assert model.call_index == 2


def test_terminal_normalization_preserves_provider_metadata():
    from voidx.agent.adapters.langgraph.runtime.turn_control import normalize_terminal_message

    pending = AIMessage(
        content="answer",
        id="message-1",
        name="assistant",
        response_metadata={"model": "test-model", "finish_reason": "stop"},
        usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        additional_kwargs={"provider_field": "value", "tool_calls": ["internal"]},
    )

    normalized = normalize_terminal_message(pending)

    assert normalized.content == "answer"
    assert normalized.id == "message-1"
    assert normalized.name == "assistant"
    assert normalized.response_metadata == pending.response_metadata
    assert normalized.usage_metadata == pending.usage_metadata
    assert normalized.additional_kwargs == {"provider_field": "value"}
    assert normalized.tool_calls == []


@pytest.mark.asyncio
async def test_long_plain_text_after_tools_commits_without_lifecycle_followup(tmp_path, monkeypatch):
    text = "\n".join(f"Line {index}." for index in range(1, 5))
    model = ScriptedStreamingModel([[_text_chunk(text)]])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [
            HumanMessage(content="hello"),
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "read",
                    "args": {"file_path": "x.py"},
                    "id": "tc-prior",
                    "type": "tool_call",
                }],
            ),
            ToolMessage(content="ok", tool_call_id="tc-prior", name="read"),
        ],
        "step_count": 0,
        "persona": "coordinate",
        "turn_state": "initial",
    })

    assert result["turn_state"] == "committed"
    assert result["messages"][0].content == text
    assert model.call_index == 1


def test_turn_tool_definition_is_the_new_flat_schema():
    definition = TURN_TOOL_DEFINITION["function"]
    assert definition["name"] == "turn_init"
    assert definition["strict"] is True
    assert definition["parameters"] == {
        "type": "object",
        "properties": {"goal": {"type": "string"}},
        "required": ["goal"],
        "additionalProperties": False,
    }
