"""Tests for call_llm tools and convergence."""

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from rich.console import Console


from voidx.agent.adapters.langgraph.runtime.streaming import stream_llm as _stream_llm
from voidx.agent.adapters.langgraph.execution import LangGraphExecution
from tests.langgraph_execution import make_langgraph_execution
from voidx.tooling.adapters.mcp import McpGatewayTool
from voidx.agent.adapters.langgraph.runtime.convergence import is_step_hint_message
from voidx.agent.adapters.langgraph.runtime.topology import latest_user_text
from voidx.agent.application.runtime_context import RuntimeContextBuilder
from voidx.agent.domain.task.state import GoalSpec, TaskState
from voidx.agent.domain.task.todo import TodoRunState
from voidx.agent.domain.automation.workflow import WorkflowRoute
from voidx.config import Config
from voidx.llm.domain.model import ModelConfig
from voidx.llm.compaction import CompactionSelection
from voidx.llm.message_markers import is_guidance_message
from voidx.agent.adapters.persistence.context_frame_repository import load_context_frames
from voidx.agent.adapters.persistence.message_rows import messages_from_rows
from voidx.agent.adapters.persistence.session_repository import (
    MessageRow,
    create_session,
    delete_session,
    load_messages,
    save_message,
)
from voidx.presentation.output.console import StreamingRenderer
from voidx.presentation.output.dock import ANSI_LINE_PREFIX, BottomInputDock, set_dock
from voidx.presentation.output.events import (
    AnsiAppended,
    DockEventConsumer,
    GuidanceCommitted,
    GuidanceSubmitted,
    MessageAppended,
    StatusFinished,
    StatusUpdated,
    ui_events,
)
from voidx.agent.application.automation.workflow.runtime import WorkflowRunState, WorkflowRunStatus
from tests.test_agent.adapters.langgraph.runtime.stream_llm_helpers import (
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
from voidx.agent.domain.automation.loop import LOOP_PROFILE
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.adapters.langgraph.runtime.thread_context import (
    ThreadExecutionState,
    _CURRENT_THREAD_EXECUTION_STATE,
    bind_thread_execution_context,
)

@pytest.mark.asyncio
async def test_call_llm_guidance_does_not_create_main_agent_convergence_hint(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    graph = make_langgraph_execution(
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
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

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

    graph = make_langgraph_execution(
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
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

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

    graph = make_langgraph_execution(
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
async def test_call_llm_user_guidance_persists_as_marked_human_message(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)
    session = await create_session(workspace=str(tmp_path))
    try:
        graph = make_langgraph_execution(
            Config(
                model=ModelConfig(provider="mimo", model="mimo-v2.5"),
                workspace=str(tmp_path),
            ),
            api_key="test",
            session=session,
        )
        graph.model = TrackingStreamingModel()

        graph.submit_guidance("Keep the API backwards compatible", source="user")
        await graph._call_llm({
            "messages": [HumanMessage(content="finish the task")],
            "step_count": 1,
            "persona": "voidx",
        })

        rows = await load_messages(session.id)
        assert len(rows) == 1
        assert rows[0].role == "user"
        assert rows[0].content == "Keep the API backwards compatible"
        hydrated = messages_from_rows(rows)
        assert len(hydrated) == 1
        assert is_guidance_message(hydrated[0])
        assert latest_user_text([HumanMessage(content="original request"), hydrated[0]]) == "original request"
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_call_llm_guidance_is_thread_scoped(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)
    session_a = await create_session(workspace=str(tmp_path))
    session_b = await create_session(workspace=str(tmp_path))
    try:
        graph = make_langgraph_execution(
            Config(
                model=ModelConfig(provider="mimo", model="mimo-v2.5"),
                workspace=str(tmp_path),
            ),
            api_key="test",
            session=session_a,
        )
        model_b = TrackingStreamingModel()
        graph.model = model_b

        graph.submit_guidance(
            "Only thread A should see this",
            source="user",
            thread_id="thread-a",
            session_id=session_a.id,
        )

        async with bind_thread_execution_context(
            graph,
            session_id=session_b.id,
            thread_id="thread-b",
            turn_context=TurnExecutionContext(
                thread_id="thread-b",
                session_id=session_b.id,
                workspace=str(tmp_path),
            ),
        ):
            await graph._call_llm({
                "messages": [HumanMessage(content="thread B request")],
                "step_count": 1,
                "persona": "voidx",
            })

        assert model_b.messages is not None
        assert not any(
            is_guidance_message(message)
            and str(message.content) == "Only thread A should see this"
            for message in model_b.messages
        )

        model_a = TrackingStreamingModel()
        graph.model = model_a
        async with bind_thread_execution_context(
            graph,
            session_id=session_a.id,
            thread_id="thread-a",
            turn_context=TurnExecutionContext(
                thread_id="thread-a",
                session_id=session_a.id,
                workspace=str(tmp_path),
            ),
        ):
            await graph._call_llm({
                "messages": [HumanMessage(content="thread A request")],
                "step_count": 1,
                "persona": "voidx",
            })

        assert model_a.messages is not None
        assert any(
            is_guidance_message(message)
            and str(message.content) == "Only thread A should see this"
            for message in model_a.messages
        )
    finally:
        await delete_session(session_a.id)
        await delete_session(session_b.id)


@pytest.mark.asyncio
async def test_call_llm_guidance_isolated_between_threads_in_same_session(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)
    session = await create_session(workspace=str(tmp_path))
    try:
        graph = make_langgraph_execution(
            Config(
                model=ModelConfig(provider="mimo", model="mimo-v2.5"),
                workspace=str(tmp_path),
            ),
            api_key="test-key",
            session=session,
        )
        async with bind_thread_execution_context(
            graph,
            session_id=session.id,
            thread_id="thread-a",
        ):
            pass
        graph.submit_guidance(
            "Only thread A should see this",
            source="user",
            thread_id="thread-a",
            session_id=session.id,
        )

        model_b = TrackingStreamingModel()
        graph.model = model_b
        async with bind_thread_execution_context(
            graph,
            session_id=session.id,
            thread_id="thread-b",
        ):
            await graph._call_llm({
                "messages": [HumanMessage(content="thread B request")],
                "step_count": 1,
                "persona": "voidx",
            })

        assert model_b.messages is not None
        assert not any(
            is_guidance_message(message)
            and str(message.content) == "Only thread A should see this"
            for message in model_b.messages
        )

        model_a = TrackingStreamingModel()
        graph.model = model_a
        async with bind_thread_execution_context(
            graph,
            session_id=session.id,
            thread_id="thread-a",
        ):
            await graph._call_llm({
                "messages": [HumanMessage(content="thread A request")],
                "step_count": 1,
                "persona": "voidx",
            })

        assert model_a.messages is not None
        assert any(
            is_guidance_message(message)
            and str(message.content) == "Only thread A should see this"
            for message in model_a.messages
        )
    finally:
        await delete_session(session.id)
@pytest.mark.asyncio
async def test_call_llm_context_frame_records_no_main_agent_convergence_hint(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)
    session = await create_session(workspace=str(tmp_path))
    try:
        graph = make_langgraph_execution(
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
    graph = make_langgraph_execution(Config(workspace=str(tmp_path)), api_key=None)

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
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    graph = make_langgraph_execution(
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
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    graph = make_langgraph_execution(
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
    assert "loop" not in tool_names


@pytest.mark.asyncio
async def test_call_llm_default_profile_does_not_bind_loop_tool(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    graph = make_langgraph_execution(
        Config(
            model=ModelConfig(provider="openai", model="gpt-4o"),
            workspace=str(tmp_path),
        ),
        api_key="test-key",
    )
    model = TrackingStreamingModel()
    graph.model = model

    await graph._call_llm({
        "messages": [HumanMessage(content="hi")],
        "step_count": 0,
        "persona": "coordinate",
    })

    tool_names = [tool["function"]["name"] for tool in model.bound_tools]
    assert "loop" not in tool_names



@pytest.mark.asyncio
async def test_call_llm_injects_loop_only_for_loop_profile(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    graph = make_langgraph_execution(
        Config(
            model=ModelConfig(provider="openai", model="gpt-4o"),
            workspace=str(tmp_path),
        ),
        api_key="test-key",
    )
    model = TrackingStreamingModel()
    graph.model = model
    turn_context = TurnExecutionContext(
        thread_id="loop:test:gen",
        session_id="loop:test",
        runtime_profile=LOOP_PROFILE,
        workspace=str(tmp_path),
    )
    thread_state = ThreadExecutionState(
        thread_id="loop:test:gen",
        turn_context=turn_context,
        runtime_profile=LOOP_PROFILE,
        workspace=str(tmp_path),
    )
    token = _CURRENT_THREAD_EXECUTION_STATE.set(thread_state)
    try:
        await graph._call_llm({
            "messages": [HumanMessage(content="[loop] 每三秒喊我吃饭")],
            "step_count": 0,
            "persona": "voidx",
        })
    finally:
        _CURRENT_THREAD_EXECUTION_STATE.reset(token)

    tool_names = [tool["function"]["name"] for tool in model.bound_tools]
    assert tool_names.count("loop") == 1


@pytest.mark.asyncio
async def test_call_llm_keeps_bound_tools_fixed_across_active_workflow_node(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    graph = make_langgraph_execution(
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
async def test_call_llm_refreshes_current_task_state_from_latest_state(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    async def no_save_context_frame(**_kwargs):
        return None

    monkeypatch.setattr(graph_module, "save_main_context_frame", no_save_context_frame)
    graph = make_langgraph_execution(
        Config(
            model=ModelConfig(provider="openai", model="gpt-4o"),
            workspace=str(tmp_path),
        ),
        api_key="test-key",
    )
    graph._session = SimpleNamespace(id="session-current-task-state-refresh")
    graph.model = TrackingStreamingModel()
    graph._last_context_builder = RuntimeContextBuilder(
        config=graph.config,
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="coordinate",
        interaction_mode="auto",
        task_state=TaskState(current_goal=GoalSpec(desc="old goal")),
    )
    todo_state = TodoRunState.model_validate({
        "summary": "0/1 done · 1 active · 0 pending",
        "total": 1,
        "done": 0,
        "active": 1,
        "pending": 0,
        "active_items": [
            {"id": "sync", "content": "refresh current task state", "status": "active"},
        ],
        "items": [
            {"id": "sync", "content": "refresh current task state", "status": "active"},
        ],
    })
    latest_task_state = TaskState(
        current_goal=GoalSpec(desc="new goal"),
        workflow_route=WorkflowRoute(join="tdd", leave="verify"),
        workflow_runs={
            "tdd": WorkflowRunState(name="tdd", status=WorkflowRunStatus.ACTIVE),
        },
        todo_state=todo_state,
    )

    await graph._call_llm({
        "messages": [HumanMessage(content="continue")],
        "step_count": 1,
        "persona": "implement",
        "turn_state": "running",
        "task_state": latest_task_state.model_dump(mode="json"),
        "todo_state": todo_state.model_dump(mode="json"),
    })

    prompt = "\n".join(str(message.content) for message in graph.model.messages)
    assert "Current persona: implement" in prompt
    assert "Goal: new goal" in prompt
    assert "Workflow route: tdd -> verify" in prompt
    assert "Active workflows: tdd" in prompt
    assert "Todo: 0/1 done · 1 active · 0 pending" in prompt
    assert "active sync: refresh current task state" in prompt
    assert "old goal" not in prompt




@pytest.mark.asyncio
async def test_call_llm_refreshes_current_task_state_after_pressure_rebuild(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module
    from voidx.agent.adapters.langgraph.runtime.context_pressure import ContextPressureDecision
    from voidx.agent.domain.task.intent import TaskIntent

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)
    monkeypatch.setattr(
        graph_module,
        "evaluate_context_pressure",
        lambda *_args, **_kwargs: ContextPressureDecision(
            over_soft=True,
            over_hard=True,
            can_compact=False,
            pressure_level="hard",
            should_inject=True,
            turn_id="turn-pressure",
            turn_count=1,
            pre_tokens=90_000,
            soft_threshold=75_000,
            hard_threshold=90_000,
            reason="hard_threshold",
        ),
    )
    graph = make_langgraph_execution(
        Config(
            model=ModelConfig(provider="openai", model="gpt-4o"),
            workspace=str(tmp_path),
        ),
        api_key="test-key",
    )
    graph.model = TrackingStreamingModel()
    graph._last_context_builder = RuntimeContextBuilder(
        config=graph.config,
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="coordinate",
        interaction_mode="auto",
        workflow_runs=[
            WorkflowRunState(name="brainstorm", status=WorkflowRunStatus.ACTIVE),
        ],
        active_workflow_summaries=["brainstorm (old trigger)"],
        task_state=TaskState(current_goal=GoalSpec(desc="old goal")),
    )
    todo_state = TodoRunState.model_validate({
        "summary": "0/1 done · 1 active · 0 pending",
        "total": 1,
        "done": 0,
        "active": 1,
        "pending": 0,
        "active_items": [
            {"id": "sync", "content": "refresh after pressure", "status": "active"},
        ],
        "items": [
            {"id": "sync", "content": "refresh after pressure", "status": "active"},
        ],
    })
    latest_task_state = TaskState(
        current_intent=TaskIntent.GENERAL,
        current_goal=GoalSpec(desc="new pressure goal"),
        workflow_route=WorkflowRoute(join="tdd", leave="verify"),
        workflow_runs={
            "tdd": WorkflowRunState(name="tdd", status=WorkflowRunStatus.ACTIVE),
        },
        todo_state=todo_state,
    )

    await graph._call_llm({
        "messages": [HumanMessage(id="turn-pressure", content="continue")],
        "step_count": 1,
        "persona": "implement",
        "turn_state": "running",
        "task_state": latest_task_state.model_dump(mode="json"),
        "todo_state": todo_state.model_dump(mode="json"),
    })

    prompt = "\n".join(str(message.content) for message in graph.model.messages)
    assert prompt.count("## Current Task State") == 1
    assert "Current persona: implement" in prompt
    assert "Intent: general" in prompt
    assert "Turn state: running" in prompt
    assert "Goal: new pressure goal" in prompt
    assert "Workflow route: tdd -> verify" in prompt
    assert "Active workflows: tdd" in prompt
    assert "Todo: 0/1 done · 1 active · 0 pending" in prompt
    assert "active sync: refresh after pressure" in prompt
    assert "old goal" not in prompt
    assert "brainstorm (old trigger)" not in prompt



@pytest.mark.asyncio
async def test_orchestrator_sees_mcp_gateway(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    graph = make_langgraph_execution(
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
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    graph = make_langgraph_execution(
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
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    graph = make_langgraph_execution(
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


@pytest.mark.asyncio
async def test_finalize_warns_about_running_child_runs(tmp_path):
    from voidx.agent.adapters.persistence.session_repository import SessionInfo

    session = SessionInfo(id="session-finalize-warning", workspace=str(tmp_path))
    graph = make_langgraph_execution(Config(workspace=str(tmp_path)), api_key=None, session=session)
    gateway = graph.agent_gateway
    root_id = gateway.ensure_root(session.id)
    release = asyncio.Event()

    async def runner(_run_id: str) -> str:
        await release.wait()
        return "late"

    child = await gateway.spawn(
        session_id=session.id,
        parent_run_id=root_id,
        agent_name="child",
        description="background child",
        runner=runner,
    )

    result = await graph._finalize({
        "messages": [AIMessage(content="Here is the final answer with enough detail.")],
        "convergence_forced": False,
    })

    assert result["messages"], "finalize should surface running background child runs"
    warning = result["messages"][-1]
    assert child.run_id in warning.content
    assert "wait" in warning.content and "cancel" in warning.content
    assert is_guidance_message(warning)

    release.set()
    await gateway.close_all()

    settled = await graph._finalize({
        "messages": [AIMessage(content="Here is the final answer with enough detail.")],
        "convergence_forced": False,
    })
    assert settled == {}
