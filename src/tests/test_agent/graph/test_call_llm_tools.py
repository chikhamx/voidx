"""Tests for call_llm tools and convergence."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from rich.console import Console


from voidx.agent.infrastructure.langgraph.runtime.streaming import stream_llm as _stream_llm
from voidx.agent.infrastructure.langgraph.execution import LangGraphExecution
from voidx.agent.infrastructure.langgraph.runtime.convergence import is_step_hint_message
from voidx.agent.runtime_context import RuntimeContextBuilder
from voidx.runtime.task_state import TaskState, TodoRunState
from voidx.config import Config, ModelConfig
from voidx.llm.compaction import CompactionSelection
from voidx.llm.message_markers import is_guidance_message
from voidx.memory.context_frames import load_context_frames
from voidx.memory.session import MessageRow, create_session, delete_session, save_message
from voidx.ui.output.console import StreamingRenderer
from voidx.ui.output.dock import ANSI_LINE_PREFIX, BottomInputDock, set_dock
from voidx.ui.output.events import (
    AnsiAppended,
    DockEventConsumer,
    GuidanceCommitted,
    GuidanceSubmitted,
    MessageAppended,
    StatusFinished,
    StatusUpdated,
    ui_events,
)
from voidx.workflow.runtime import WorkflowRunState, WorkflowRunStatus
from tests.test_agent.graph.stream_llm_helpers import (
    _plain,
    FakeStreamingModel,
    FakeUsageStreamingModel,
    FakeDuplicatedReasoningStreamingModel,
    FakeDsmlStreamingModel,
    FakeMalformedDsmlStreamingModel,
    TrackingStreamingModel,
    FailsOnceStreamingModel,
    FakeRenderer,
)

@pytest.mark.asyncio
async def test_call_llm_guidance_does_not_create_main_agent_convergence_hint(tmp_path, monkeypatch):
    import voidx.agent.infrastructure.langgraph.execution as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    graph = LangGraphExecution(
        Config(
            model=ModelConfig(provider="mimo", model="mimo-v2.5"),
            workspace=str(tmp_path),
        ),
        api_key=None,
    )
    model = TrackingStreamingModel()
    graph.model = model

    graph.submit_guidance("Use TypeScript for the implementation")
    result = await graph._call_llm({
        "messages": [HumanMessage(content="finish the task")],
        "step_count": 49,
        "persona": "voidx",
    })

    assert result["convergence_forced"] is False
    assert model.messages is not None
    assert is_guidance_message(model.messages[-1])
    assert not any(is_step_hint_message(message) for message in model.messages)


@pytest.mark.asyncio
async def test_call_llm_guard_guidance_stays_hidden_from_ui_events(tmp_path, monkeypatch):
    import voidx.agent.infrastructure.langgraph.execution as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    class RecordingEvents:
        def __init__(self) -> None:
            self.emitted = []

        def emit_direct(self, event) -> bool:
            self.emitted.append(event)
            return True

        async def emit(self, event) -> bool:
            self.emitted.append(event)
            return True

    graph = LangGraphExecution(
        Config(
            model=ModelConfig(provider="mimo", model="mimo-v2.5"),
            workspace=str(tmp_path),
        ),
        api_key=None,
    )
    events = RecordingEvents()
    graph._ui = SimpleNamespace(
        console=Console(file=sys.stdout),
        events=events,
        ui=SimpleNamespace(
            print=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: None,
        ),
        via_events=lambda: True,
    )
    model = TrackingStreamingModel()
    graph.model = model

    graph.submit_guidance("No meaningful progress has been detected", source="guard")
    result = await graph._call_llm({
        "messages": [HumanMessage(content="finish the task")],
        "step_count": 1,
        "persona": "voidx",
    })

    assert result["step_count"] == 2
    assert model.messages is not None
    assert is_guidance_message(model.messages[-1])
    assert model.messages[-1].content == "No meaningful progress has been detected"
    assert not any(isinstance(event, GuidanceSubmitted | MessageAppended | GuidanceCommitted) for event in events.emitted)


@pytest.mark.asyncio
async def test_call_llm_user_guidance_commits_preview_without_persistent_message(tmp_path, monkeypatch):
    import voidx.agent.infrastructure.langgraph.execution as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    class RecordingEvents:
        def __init__(self) -> None:
            self.emitted = []

        def emit_direct(self, event) -> bool:
            self.emitted.append(event)
            return True

        async def emit(self, event) -> bool:
            self.emitted.append(event)
            return True

    graph = LangGraphExecution(
        Config(
            model=ModelConfig(provider="mimo", model="mimo-v2.5"),
            workspace=str(tmp_path),
        ),
        api_key=None,
    )
    events = RecordingEvents()
    graph._ui = SimpleNamespace(
        console=Console(file=sys.stdout),
        events=events,
        ui=SimpleNamespace(
            print=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: None,
        ),
        via_events=lambda: True,
    )
    model = TrackingStreamingModel()
    graph.model = model

    graph.submit_guidance("先写 spedc文档", source="user")
    events.emitted.clear()
    result = await graph._call_llm({
        "messages": [HumanMessage(content="finish the task")],
        "step_count": 1,
        "persona": "voidx",
    })

    assert result["step_count"] == 2
    assert model.messages is not None
    assert is_guidance_message(model.messages[-1])
    assert model.messages[-1].content == "先写 spedc文档"
    assert events.emitted == [GuidanceCommitted(text="先写 spedc文档")]


@pytest.mark.asyncio
async def test_call_llm_context_frame_records_no_main_agent_convergence_hint(tmp_path, monkeypatch):
    import voidx.agent.infrastructure.langgraph.execution as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)
    session = await create_session(workspace=str(tmp_path))
    try:
        graph = LangGraphExecution(
            Config(
                model=ModelConfig(provider="mimo", model="mimo-v2.5"),
                workspace=str(tmp_path),
            ),
            api_key=None,
            session=session,
        )
        graph.model = FakeStreamingModel()

        await graph._call_llm({
            "messages": [HumanMessage(content="finish the task")],
            "step_count": 49,
            "persona": "voidx",
        })

        frames = await load_context_frames(session.id)
        assert frames[0].metadata["convergence_forced"] is False
        assert frames[0].metadata["convergence_hint_count"] == 0
        assert all("FINAL response step" not in message["content"] for message in frames[0].messages)
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_finalize_uses_fallback_only_for_invalid_forced_convergence(tmp_path):
    graph = LangGraphExecution(Config(workspace=str(tmp_path)), api_key=None)

    normal = await graph._finalize({
        "messages": [AIMessage(content="ok")],
        "convergence_forced": False,
    })
    fallback = await graph._finalize({
        "messages": [
            HumanMessage(content="Fix src/voidx/agent/graph/core.py"),
            AIMessage(content=""),
        ],
        "goal": "",
        "tool_results": {"tc1": "read src/voidx/agent/graph/core.py"},
        "step_count": 50,
        "convergence_forced": True,
    })
    valid_forced = await graph._finalize({
        "messages": [AIMessage(content="Here is the final result with enough detail.")],
        "convergence_forced": True,
    })
    valid_forced_with_tool_tail = await graph._finalize({
        "messages": [
            AIMessage(content="Here is the final result with enough detail."),
            ToolMessage(content="late tool result", tool_call_id="tc_tail"),
        ],
        "convergence_forced": True,
    })

    assert normal == {}
    assert valid_forced == {}
    assert valid_forced_with_tool_tail == {}
    assert "src/voidx/agent/graph/core.py" in fallback["messages"][0].content
    assert "Pending" in fallback["messages"][0].content


@pytest.mark.asyncio
async def test_call_llm_filters_lsp_tools_when_no_lsp_server_is_available(tmp_path, monkeypatch):
    import voidx.agent.infrastructure.langgraph.execution as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    graph = LangGraphExecution(
        Config(
            model=ModelConfig(provider="openai", model="gpt-4o"),
            workspace=str(tmp_path),
        ),
        api_key=None,
    )
    graph._lsp_manager = SimpleNamespace(
        doctor=lambda: [SimpleNamespace(enabled=True, available=False)]
    )
    model = TrackingStreamingModel()
    graph.model = model

    await graph._call_llm({
        "messages": [HumanMessage(content="hi")],
        "step_count": 0,
        "persona": "voidx",
    })

    tool_names = [tool["function"]["name"] for tool in model.bound_tools]
    assert tool_names
    assert not any(name.startswith("lsp_") for name in tool_names)


@pytest.mark.asyncio
async def test_available_tool_ids_no_longer_filter_llm_tools(tmp_path, monkeypatch):
    import voidx.agent.infrastructure.langgraph.execution as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    graph = LangGraphExecution(
        Config(
            model=ModelConfig(provider="openai", model="gpt-4o"),
            workspace=str(tmp_path),
        ),
        api_key=None,
    )
    model = TrackingStreamingModel()
    graph.model = model

    await graph._call_llm({
        "messages": [HumanMessage(content="hi")],
        "step_count": 0,
        "persona": "coordinate",
        "available_tool_ids": ["read", "search"],
    })

    tool_names = [tool["function"]["name"] for tool in model.bound_tools]
    assert "read" in tool_names
    assert "search" in tool_names
    assert "mcp" in tool_names


@pytest.mark.asyncio
async def test_call_llm_keeps_bound_tools_fixed_across_active_workflow_node(tmp_path, monkeypatch):
    import voidx.agent.infrastructure.langgraph.execution as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    graph = LangGraphExecution(
        Config(
            model=ModelConfig(provider="openai", model="gpt-4o"),
            workspace=str(tmp_path),
        ),
        api_key=None,
    )
    model = TrackingStreamingModel()
    graph.model = model
    task_state = TaskState(
        workflow_runs={
            "brainstorm": WorkflowRunState(name="brainstorm", status=WorkflowRunStatus.ACTIVE)
        }
    )

    await graph._call_llm({
        "messages": [HumanMessage(content="hi")],
        "step_count": 0,
        "persona": "coordinate",
        "task_state": task_state.model_dump(mode="json"),
    })

    tool_names = [tool["function"]["name"] for tool in model.bound_tools]
    assert "read" in tool_names
    assert "search" in tool_names
    assert "clarify" in tool_names
    assert "workflow" in tool_names
    assert ("bash" if os.name != "nt" else "powershell") in tool_names
    assert "manage" in tool_names
    assert "file" not in tool_names
    assert "write" in tool_names
    assert "replace" in tool_names
    assert "line" not in tool_names
    assert "insert" not in tool_names
    assert "edit" not in tool_names



@pytest.mark.asyncio
async def test_orchestrator_sees_mcp_gateway(tmp_path, monkeypatch):
    import voidx.agent.infrastructure.langgraph.execution as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    graph = LangGraphExecution(
        Config(
            model=ModelConfig(provider="openai", model="gpt-4o"),
            workspace=str(tmp_path),
        ),
        api_key=None,
    )
    model = TrackingStreamingModel()
    graph.model = model

    await graph._call_llm({
        "messages": [HumanMessage(content="hi")],
        "step_count": 0,
        "persona": "voidx",
    })

    tool_names = [tool["function"]["name"] for tool in model.bound_tools]
    assert "mcp" in tool_names


@pytest.mark.asyncio
async def test_runtime_persona_does_not_change_agent_mcp_gateway_visibility(tmp_path, monkeypatch):
    import voidx.agent.infrastructure.langgraph.execution as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    graph = LangGraphExecution(
        Config(
            model=ModelConfig(provider="openai", model="gpt-4o"),
            workspace=str(tmp_path),
        ),
        api_key=None,
    )
    model = TrackingStreamingModel()
    graph.model = model

    await graph._call_llm({
        "messages": [HumanMessage(content="hi")],
        "step_count": 0,
        "persona": "explore",
    })

    tool_names = [tool["function"]["name"] for tool in model.bound_tools]
    assert "read" in tool_names
    assert "mcp" in tool_names


@pytest.mark.asyncio
async def test_call_llm_keeps_lsp_tools_when_a_lsp_server_is_available(tmp_path, monkeypatch):
    import voidx.agent.infrastructure.langgraph.execution as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    graph = LangGraphExecution(
        Config(
            model=ModelConfig(provider="openai", model="gpt-4o"),
            workspace=str(tmp_path),
        ),
        api_key=None,
    )
    graph._lsp_manager = SimpleNamespace(
        has_available_server=lambda: True
    )
    model = TrackingStreamingModel()
    graph.model = model

    await graph._call_llm({
        "messages": [HumanMessage(content="hi")],
        "step_count": 0,
        "persona": "voidx",
    })

    tool_names = [tool["function"]["name"] for tool in model.bound_tools]
    assert "lsp" in tool_names
