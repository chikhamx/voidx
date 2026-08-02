"""Integration tests for turn control inside _call_llm."""

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

from voidx.agent.infrastructure.langgraph.execution import LangGraphExecution
from voidx.agent.infrastructure.langgraph.runtime.turn_control import TURN_TOOL_DEFINITION
from voidx.config import Config, ModelConfig
from tests.test_infrastructure.runtime.stream_llm_helpers import FakeRenderer


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


def _turn_args(operation: str = "stop", intent: str = "", goal: str = "") -> dict[str, str]:
    return {"operation": operation, "params": None if operation == "stop" else {"intent": intent, "goal": goal}}


def _turn_call_chunk() -> AIMessageChunk:
    return AIMessageChunk(
        content="",
        tool_calls=[{"name": "turn", "args": _turn_args(), "id": "tc1", "type": "tool_call"}],
    )


def _turn_call_with_decision_chunk(decision: str) -> AIMessageChunk:
    return AIMessageChunk(
        content="",
        tool_calls=[{"name": "turn", "args": {"decision": decision}, "id": f"tc-{decision}", "type": "tool_call"}],
    )


def _turn_stop_with_text_chunk(text: str = "unexpected text") -> AIMessageChunk:
    return AIMessageChunk(
        content=text,
        tool_calls=[{"name": "turn", "args": _turn_args(), "id": "tc-stop-text", "type": "tool_call"}],
    )



def _turn_start_chunk(intent: str = "coding", goal: str = "Fix the issue") -> AIMessageChunk:
    return AIMessageChunk(
        content="",
        tool_calls=[{
            "name": "turn",
            "args": _turn_args(operation="start", intent=intent, goal=goal),
            "id": "tc-start",
            "type": "tool_call",
        }],
    )
def _text_chunk(text: str) -> AIMessageChunk:
    return AIMessageChunk(content=text)


def _regular_tool_chunk() -> AIMessageChunk:
    return AIMessageChunk(
        content="",
        tool_calls=[{"name": "read", "args": {"file_path": "x.py"}, "id": "tc2", "type": "tool_call"}],
    )


def test_turn_tool_definition_describes_start_and_stop_usage():
    definition = TURN_TOOL_DEFINITION["function"]
    description = definition["description"]

    assert "At turn start" in description
    assert "At turn end" in description
    assert "Do not combine turn with other tool calls" in description
    assert definition["parameters"]["properties"]["operation"]["description"] == (
        "start declares intent and goal; stop commits the pending final answer."
    )


def _mixed_chunk() -> AIMessageChunk:
    return AIMessageChunk(
        content="",
        tool_calls=[
            {"name": "read", "args": {"file_path": "x.py"}, "id": "tc3", "type": "tool_call"},
            {"name": "turn", "args": _turn_args(), "id": "tc4", "type": "tool_call"},
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
    import voidx.agent.infrastructure.langgraph.runtime.llm_turn as graph_module
    monkeypatch.setattr(graph_module, "StreamingRenderer", renderer_cls)

    graph = LangGraphExecution(
        Config(
            model=ModelConfig(provider=provider, model="test-model"),
            workspace=str(tmp_path),
        ),
        api_key="test-key",
    )
    graph.model = model
    return graph


@pytest.mark.asyncio
async def test_turn_start_accepts_goal_then_continues_to_stop(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_turn_start_chunk(goal="Implement turn start")],
        [_text_chunk("Done.")],
        [_turn_call_chunk()],
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
    assert result["task_state"]["current_goal"] == {"desc": "Implement turn start"}
    assert model.call_index == 3
    assert any(
        getattr(msg, "name", "") == "turn"
        and "Check the active workflow" in str(msg.content)
        for msg in model.received_messages[1]
    )


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
                    "args": _turn_args(),
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


# ── Test 4: first plain-text answer can complete immediately ────────────────


@pytest.mark.asyncio
async def test_first_plain_text_commits_without_start_prompt(tmp_path, monkeypatch):
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
    assert result["turn_state"] == "committed"
    assert model.call_index == 1
    assert len(model.received_messages) == 1


@pytest.mark.asyncio
async def test_initial_plain_text_keeps_full_tool_set_without_reprompt(tmp_path, monkeypatch):
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
    assert model.call_index == 1
    assert len(model.received_messages) == 1
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
        "turn_state": "running",
    })

    assert result["messages"][0].tool_calls
    assert result["messages"][0].tool_calls[0]["name"] == "read"
    assert result["messages"][0].content == ""
    assert model.call_index == 2


@pytest.mark.asyncio
async def test_invalid_turn_with_text_commits_pending_without_retry(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_text_chunk("Provisional answer.")],
        [_turn_stop_with_text_chunk("extra text")],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
        "turn_state": "running",
    })

    assert result["messages"][0].content == "Provisional answer."
    assert model.call_index == 2


# ── Test 5: second consecutive plain-text exits without another prompt ──────


@pytest.mark.asyncio
async def test_initial_plain_text_commits_without_hidden_ui_followups(tmp_path, monkeypatch):
    RecordingRenderer.reset()
    model = ScriptedStreamingModel([
        [_text_chunk("First provisional.")],
        [_text_chunk("Second provisional.")],
        [_text_chunk("")],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch, renderer_cls=RecordingRenderer)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
    })

    visible_text = "".join(RecordingRenderer.visible_text)
    assert result["messages"][0].content == "First provisional."
    assert "First provisional." in visible_text
    assert "Second provisional." not in visible_text
    assert RecordingRenderer.headless_values[0] is False
    assert RecordingRenderer.headless_values == [False]
    assert model.call_index == 1


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
        "turn_state": "running",
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
        [malformed_turn],
        [_text_chunk("answer")],
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
    from voidx.agent.infrastructure.langgraph.runtime.turn_control import normalize_terminal_message

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


# ── Test: turn(start) re-renders context with Turn state: running ──────────


@pytest.mark.asyncio
async def test_turn_start_rerenders_context_with_running_state(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_turn_start_chunk(goal="Implement turn start")],
        [_text_chunk("Done.")],
        [_turn_call_chunk()],
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

    second_round_messages = model.received_messages[1]
    task_context_text = "\n".join(
        str(getattr(msg, "content", "")) for msg in second_round_messages
    )
    assert "Turn state: running" in task_context_text
    assert "Implement turn start" in task_context_text


# ── Test: TURN_START_PROMPT injected once, then TURN_STOP_PROMPT ────────────


@pytest.mark.asyncio
async def test_start_prompt_injected_once_then_stop_prompt(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_text_chunk("First provisional.")],
        [_text_chunk("Second provisional.")],
        [_text_chunk("Third provisional.")],
        [_turn_call_chunk()],
    ])
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

    round1_messages = model.received_messages[1]
    round1_text = "\n".join(str(getattr(msg, "content", "")) for msg in round1_messages)
    assert "operation='start'" in round1_text

    round2_messages = model.received_messages[2]
    round2_text = "\n".join(str(getattr(msg, "content", "")) for msg in round2_messages)
    assert "operation='stop'" in round2_text
    assert "final answer" in round2_text
    assert "regular tool" in round2_text

    assert result["messages"][0].content == "Second provisional."
    assert model.call_index == 3



# ── Test: initial state, >3 lines after tools, auto-commit without TURN_START_PROMPT ─


@pytest.mark.asyncio
async def test_initial_long_plain_text_after_tools_auto_commits(tmp_path, monkeypatch):
    """In initial state, >3 lines of plain text after tool calls should auto-commit
    without injecting TURN_START_PROMPT retry."""
    long_text = "\n".join(f"Line {i}." for i in range(1, 5))
    model = ScriptedStreamingModel([
        [_text_chunk(long_text)],
    ])
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
    assert result["messages"][0].content == long_text
    assert model.call_index == 1
    assert len(model.received_messages) == 1


# ── Test: running state, exactly 3 lines triggers TURN_STOP_PROMPT (not auto-commit) ─


@pytest.mark.asyncio
async def test_running_three_line_plain_text_triggers_stop_prompt(tmp_path, monkeypatch):
    """In running state, exactly 3 lines of plain text should trigger TURN_STOP_PROMPT
    retry, not auto-commit (threshold is >3, i.e. >=4)."""
    three_line_text = "Line 1.\nLine 2.\nLine 3."
    model = ScriptedStreamingModel([
        [_text_chunk(three_line_text)],
        [_turn_call_chunk()],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
        "turn_state": "running",
    })

    assert result["turn_state"] == "committed"
    assert model.call_index == 2
    round1_messages = model.received_messages[1]
    round1_text = "\n".join(str(getattr(msg, "content", "")) for msg in round1_messages)
    assert "operation='stop'" in round1_text


@pytest.mark.asyncio
async def test_initial_empty_plain_text_retries_instead_of_committing_blank(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [AIMessageChunk(content="")],
        [_turn_start_chunk(goal="Continue the task")],
        [_text_chunk("Done after retry.")],
        [_turn_call_chunk()],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="continue")],
        "step_count": 0,
        "persona": "coordinate",
        "turn_state": "initial",
    })

    assert result["turn_state"] == "committed"
    assert result["messages"][0].content == "Done after retry."
    assert model.call_index == 4


@pytest.mark.asyncio
async def test_tool_followup_empty_plain_text_preserves_prior_provisional(tmp_path, monkeypatch):
    model = ScriptedStreamingModel([
        [_text_chunk("Starting the implementation now.")],
        [AIMessageChunk(content="")],
        [AIMessageChunk(content="")],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [
            HumanMessage(content="fix it"),
            AIMessage(
                content="I will inspect the tests.",
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

    assert result["messages"][0].content == "Starting the implementation now."
    assert model.call_index == 3
