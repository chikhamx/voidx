"""Tests for turn control prompt rules in system context."""

from tests.langgraph_execution import make_langgraph_execution
import pytest
from langchain_core.messages import HumanMessage

from voidx.agent.infrastructure.langgraph.execution import LangGraphExecution
from voidx.agent.infrastructure.langgraph.runtime.turn_control import (
    TURN_START_PROMPT,
    TURN_STOP_PROMPT,
    TURN_TOOL_DEFINITION,
)
from voidx.config import Config, ModelConfig
from tests.test_infrastructure.runtime.stream_llm_helpers import FakeRenderer, FakeStreamingModel


def _make_graph(tmp_path, monkeypatch, provider="openai"):
    import voidx.agent.infrastructure.langgraph.runtime.llm_turn as graph_module
    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    graph = make_langgraph_execution(
        Config(
            model=ModelConfig(provider=provider, model="test-model"),
            workspace=str(tmp_path),
        ),
        api_key="test-key",
    )
    graph.model = FakeStreamingModel()
    return graph


@pytest.mark.asyncio
async def test_turn_control_rules_are_not_repeated_in_openai_system_prompt(tmp_path, monkeypatch):
    graph = _make_graph(tmp_path, monkeypatch, provider="openai")
    state = {
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
    }
    await graph._prepare_with_stream(state)

    system_content = graph._context_cache.stable_system_content or ""
    assert "operation='start'" not in system_content
    assert "operation='stop'" not in system_content
    assert "Turn Completion Protocol" not in system_content
    assert "provisional" not in system_content.lower()


@pytest.mark.asyncio
async def test_turn_control_rules_are_not_repeated_in_deepseek_system_prompt(tmp_path, monkeypatch):
    graph = _make_graph(tmp_path, monkeypatch, provider="deepseek")
    state = {
        "messages": [HumanMessage(content="hello")],
        "step_count": 0,
        "persona": "coordinate",
    }
    await graph._prepare_with_stream(state)

    system_content = graph._context_cache.stable_system_content or ""
    assert "operation='start'" not in system_content
    assert "operation='stop'" not in system_content
    assert "Turn Completion Protocol" not in system_content


def test_turn_tool_and_runtime_prompts_own_lifecycle_protocol():
    description = TURN_TOOL_DEFINITION["function"]["description"]

    assert "operation='start'" in description
    assert "operation='stop'" in description
    assert "operation='start'" in TURN_START_PROMPT
    assert "operation='stop'" in TURN_STOP_PROMPT
