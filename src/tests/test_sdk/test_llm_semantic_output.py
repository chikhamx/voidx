"""Exercise LlmTurn's production streaming path without a UI capability."""
import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessageChunk

from voidx.agent.adapters.langgraph.runtime.llm_turn import LlmTurn
from voidx.agent.adapters.langgraph.runtime.semantic_output import SemanticOutput
from voidx.agent.application.runtime.semantic_channel import SemanticChannel


class Model:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.read = 0
        self.closed = False

    async def astream(self, messages):
        try:
            for text in ("hello", " world"):
                self.read += 1
                yield AIMessageChunk(content=text)
            if self.fail:
                raise RuntimeError("Authorization: Bearer private-secret")
        finally:
            self.closed = True


class Publisher:
    def __init__(self):
        self.events = []

    async def publish(self, event):
        self.events.append(event)


def turn(publisher):
    return LlmTurn(SimpleNamespace(semantic_output=SemanticOutput(
        publisher, session_id="s", thread_id="t", turn_id="r",
    )))


@pytest.mark.asyncio
async def test_real_stream_commits_without_ui():
    publisher = Publisher()
    model = Model()
    result = await turn(publisher).stream(model, [], "")
    assert result.content == "hello world"
    assert model.closed
    assert [e.kind for e in publisher.events] == [
        "assistant.stream_started", "assistant.chunk", "assistant.chunk", "assistant.committed",
    ]
    assert publisher.events[-1].payload.text == result.content
    assert len({e.payload.stream_id for e in publisher.events}) == 1


@pytest.mark.asyncio
async def test_real_stream_discards_and_redacts_diagnostics():
    publisher = Publisher()
    model = Model(fail=True)
    with pytest.raises(RuntimeError, match="private-secret"):
        await turn(publisher).stream(model, [], "")
    assert model.closed
    assert [e.kind for e in publisher.events][-2:] == ["assistant.discarded", "diagnostic.error"]
    assert "private-secret" not in str([e.model_dump() for e in publisher.events])
    assert not any(e.kind == "assistant.committed" for e in publisher.events)


@pytest.mark.asyncio
async def test_capacity_one_backpressure_and_cancel_closes_model():
    channel = SemanticChannel(session_id="s", thread_id="t", turn_id="r", capacity=1)
    model = Model()
    task = asyncio.create_task(turn(channel).stream(model, [], ""))
    try:
        for _ in range(10):
            await asyncio.sleep(0)
        assert model.read == 1
        assert not task.done()
        events = channel.events()
        assert (await anext(events)).kind == "assistant.stream_started"
        for _ in range(10):
            await asyncio.sleep(0)
        assert model.read == 2
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert model.closed
        await events.aclose()
        assert not channel._lock.locked()
        assert not channel._queue._putters
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_semantic_branch_never_uses_supplied_renderer_or_ui():
    from voidx.agent.adapters.langgraph.runtime.streaming import stream_llm

    class ForbiddenUi:
        def __getattr__(self, name):
            raise AssertionError(f"UI accessed: {name}")

    publisher = Publisher()
    output = turn(publisher).host.semantic_output
    result = await stream_llm(Model(), [], ForbiddenUi(), ui_port=ForbiddenUi(), semantic_output=output)
    assert result.content == "hello world"
    assert publisher.events[-1].kind == "assistant.committed"


@pytest.mark.asyncio
async def test_semantic_stream_preserves_reasoning_and_replay_sanitization():
    from langchain_core.messages import AIMessage, HumanMessage

    class ReasoningModel:
        async def astream(self, messages):
            assert len(messages) == 1
            assert isinstance(messages[0], HumanMessage)
            yield AIMessageChunk(content=[
                {"type": "thinking", "thinking": "consider"},
                {"type": "text", "text": "answer"},
            ])

    publisher = Publisher()
    result = await turn(publisher).stream(ReasoningModel(), [
        AIMessage(content=[{"type": "thinking", "thinking": "old"}]),
        HumanMessage(content="question"),
    ], "")
    assert result.content == "answer"
    commits = {e.payload.phase: e.payload.text for e in publisher.events if e.kind == "assistant.committed"}
    assert commits == {"text": "answer", "thinking": "consider"}


@pytest.mark.asyncio
async def test_legacy_branch_still_renders_once():
    from unittest.mock import Mock

    renderer = Mock()
    ui = SimpleNamespace(console=object(), streaming_renderer=Mock(return_value=renderer), events=SimpleNamespace(is_running=False))
    host = SimpleNamespace(_ui=ui, _debug=False)
    result = await LlmTurn(host).stream(Model(), [], "")
    assert result.content == "hello world"
    renderer.start.assert_called_once_with()
    assert [call.args[0] for call in renderer.feed_text.call_args_list] == ["hello", " world"]
    renderer.done.assert_called_once_with()
    renderer.discard.assert_not_called()
