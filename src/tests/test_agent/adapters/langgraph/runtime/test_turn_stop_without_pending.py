import asyncio
"""Test: turn stop without pending provisional text in running state.

When the main agent calls a child agent (e.g. review) and the child returns,
the main agent may call turn operation='stop' directly without first emitting
plain text. In that case pending_provisional is None and validate_turn_call
rejects the stop. The INVALID_TURN_PROMPT then tells the LLM to "not output
text, call turn stop" — which is contradictory because there is no pending
text to commit. This creates a dead loop that ends with a failure message
and should_continue=False, producing no user-facing text.
"""

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

from voidx.agent.infrastructure.langgraph.execution import LangGraphExecution
from tests.langgraph_execution import make_langgraph_execution
from voidx.config import Config
from voidx.llm.domain.model import ModelConfig
from voidx.presentation.output.events import AssistantStreamCommitted, AssistantStreamUpdated
from tests.test_agent.adapters.langgraph.runtime.stream_llm_helpers import FakeRenderer


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


def _turn_stop_chunk() -> AIMessageChunk:
    return AIMessageChunk(
        content="",
        tool_calls=[{
            "name": "turn",
            "args": {"operation": "stop", "params": None},
            "id": "tc1",
            "type": "tool_call",
        }],
    )


def _text_chunk(text: str) -> AIMessageChunk:
    return AIMessageChunk(content=text)


def _text_and_turn_stop_chunk(text: str) -> AIMessageChunk:
    return AIMessageChunk(
        content=text,
        tool_calls=[{
            "name": "turn",
            "args": {"operation": "stop", "params": None},
            "id": "tc1",
            "type": "tool_call",
        }],
    )


def _make_graph(tmp_path, model, monkeypatch, provider="openai"):
    import voidx.agent.infrastructure.langgraph.runtime.llm_turn as graph_module

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
async def test_turn_stop_in_running_state_without_pending_emits_text(tmp_path, monkeypatch):
    """When turn_state='running' and LLM calls turn stop without prior text,
    the agent should recover by prompting the LLM to produce text, not enter
    a dead loop that ends with a bare failure message.
    """
    model = ScriptedStreamingModel([
        # 1) LLM calls turn stop directly (no text, no pending_provisional)
        [_turn_stop_chunk()],
        # 2) After MISSING_PENDING_PROMPT, LLM outputs plain text summary
        [_text_chunk("Review completed: PASS")],
        # 3) After TURN_STOP_PROMPT, LLM calls turn stop to commit
        [_turn_stop_chunk()],
    ])
    graph = _make_graph(tmp_path, model, monkeypatch)

    result = await graph._call_llm({
        "messages": [HumanMessage(content="Run review")],
        "step_count": 1,
        "persona": "coordinate",
        "turn_state": "running",
    })

    # The agent should produce user-facing text, not a bare failure message.
    msg = result["messages"][0]
    assert isinstance(msg, AIMessage)
    assert not msg.tool_calls
    text = (msg.content or "").strip() if isinstance(msg.content, str) else ""
    assert "Review completed" in text
    assert "LLM call failed" not in text


@pytest.mark.parametrize("repair_shape", ["text_then_stop", "text_and_stop"])
@pytest.mark.asyncio
async def test_headless_repair_commit_emits_user_visible_stream(tmp_path, monkeypatch, repair_shape):
    """Gemini often repairs missing pending text by returning text + turn stop
    in one headless repair call. The committed answer must still be emitted to
    the UI; checking only result["messages"] misses the user-visible blank turn.
    """
    import voidx.agent.infrastructure.langgraph.runtime.llm_turn as graph_module

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

    class HeadlessAwareRenderer(FakeRenderer):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.headless = bool(kwargs.get("headless", False))

        def feed_text(self, text: str) -> None:
            if not self.headless:
                super().feed_text(text)

    async def fail_on_retry(delay):
        pytest.fail(f"Unexpected LLM retry with delay {delay}s")

    monkeypatch.setattr(graph_module, "StreamingRenderer", HeadlessAwareRenderer)
    monkeypatch.setattr(asyncio, "sleep", fail_on_retry)

    scripts = [[_turn_stop_chunk()]]
    if repair_shape == "text_then_stop":
        scripts.extend([[_text_chunk("Review completed: PASS")], [_turn_stop_chunk()]])
    else:
        scripts.append([_text_and_turn_stop_chunk("Review completed: PASS")])
    model = ScriptedStreamingModel(scripts)
    graph = make_langgraph_execution(
        Config(
            model=ModelConfig(provider="gemini", model="gemini-test"),
            workspace=str(tmp_path),
        ),
        api_key=None,
    )
    graph.model = model
    graph._ui = TrackingUi()

    result = await graph._call_llm({
        "messages": [HumanMessage(content="Run review")],
        "step_count": 1,
        "persona": "coordinate",
        "turn_state": "running",
    })

    assert result["messages"][0].content == "Review completed: PASS"
    stream_updates = [event for event in emitted if isinstance(event, AssistantStreamUpdated)]
    assert any("Review completed: PASS" in event.text for event in stream_updates)
    assert any(isinstance(event, AssistantStreamCommitted) for event in emitted)


@pytest.mark.asyncio
async def test_turn_stop_with_text_after_missing_pending(tmp_path, monkeypatch):
    """When MISSING_PENDING_PROMPT is injected and LLM responds with text +
    turn stop in the same message, the agent should accept the text as
    pending and commit it, not treat it as INVALID_TURN.
    """
    model = ScriptedStreamingModel([
        # 1) LLM calls turn stop directly (no text, no pending_provisional)
        [_turn_stop_chunk()],
        # 2) After MISSING_PENDING_PROMPT, LLM outputs text + turn stop together
        [_text_chunk("Review completed: PASS"), _turn_stop_chunk()],
    ])
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
    text = (msg.content or "").strip() if isinstance(msg.content, str) else ""
    assert "Review completed" in text
    assert "LLM call failed" not in text
