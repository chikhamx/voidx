"""Tests for turn control prompt rules in system context."""

import pytest
from langchain_core.messages import HumanMessage

from voidx.agent.graph import VoidXGraph
from voidx.config import Config, ModelConfig
from tests.test_agent.graph.stream_llm_helpers import FakeRenderer, FakeStreamingModel


def _make_graph(tmp_path, monkeypatch, provider="openai"):
    import voidx.agent.graph.core.llm as graph_module
    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    graph = VoidXGraph(
        Config(
            model=ModelConfig(provider=provider, model="test-model"),
            workspace=str(tmp_path),
        ),
        api_key="test-key",
    )
    graph.model = FakeStreamingModel()
    return graph


@pytest.mark.asyncio
async def test_turn_control_rules_present_for_openai(tmp_path, monkeypatch):
    graph = _make_graph(tmp_path, monkeypatch, provider="openai")
    state = {
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
    }
    await graph._prepare_with_stream(state)

    system_content = graph._context_cache.stable_system_content or ""
    assert "operation='start'" in system_content
    assert "turn state is initial" in system_content
    assert "operation='stop'" in system_content
    assert "Turn Completion Protocol" not in system_content
    assert "provisional" not in system_content.lower()


@pytest.mark.asyncio
async def test_turn_control_rules_present_for_deepseek(tmp_path, monkeypatch):
    graph = _make_graph(tmp_path, monkeypatch, provider="deepseek")
    state = {
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
    }
    await graph._prepare_with_stream(state)

    system_content = graph._context_cache.stable_system_content or ""
    assert "operation='start'" in system_content
    assert "operation='stop'" in system_content
    assert "Turn Completion Protocol" not in system_content
