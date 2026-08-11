"""Regression tests for refreshing Current Task State before every model call."""

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

from tests.langgraph_execution import make_langgraph_execution
from tests.test_agent.adapters.langgraph.runtime.stream_llm_helpers import (
    FakeRenderer,
    RepairsMalformedToolCallStreamingModel,
    TrackingStreamingModel,
)
from voidx.agent.application.runtime_context import RuntimeContextBuilder
from voidx.agent.domain.automation.workflow import (
    WorkflowRoute,
    WorkflowRunState,
    WorkflowRunStatus,
)
from voidx.agent.domain.task.state import GoalSpec, TaskState
from voidx.agent.domain.task.todo import TodoRunState
from voidx.config import Config
from voidx.llm.domain.model import ModelConfig
from voidx.tooling.domain.context import ToolExecutionContext as ToolContext
from voidx.tooling.domain.result import ToolResult


def _todo_state(content: str) -> TodoRunState:
    return TodoRunState.model_validate({
        "summary": "0/1 done · 1 active · 0 pending",
        "total": 1,
        "done": 0,
        "active": 1,
        "pending": 0,
        "active_items": [
            {"id": "sync", "content": content, "status": "active"},
        ],
        "items": [
            {"id": "sync", "content": content, "status": "active"},
        ],
    })


def _task_state(goal: str, todo_state: TodoRunState) -> TaskState:
    return TaskState(
        current_goal=GoalSpec(desc=goal),
        workflow_route=WorkflowRoute(join="tdd", leave="verify"),
        workflow_runs={
            "tdd": WorkflowRunState(name="tdd", status=WorkflowRunStatus.ACTIVE),
        },
        todo_state=todo_state,
    )


def _install_old_builder(graph, tmp_path) -> None:
    graph._last_context_builder = RuntimeContextBuilder(
        config=graph.config,
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="coordinate",
        interaction_mode="auto",
        workflow_runs=[
            WorkflowRunState(name="tdd", status=WorkflowRunStatus.ACTIVE),
        ],
        active_workflow_summaries=["tdd (old trigger)"],
        task_state=TaskState(current_goal=GoalSpec(desc="old goal")),
    )


def _assert_latest_prompt(prompt: str, *, goal: str, todo_content: str) -> None:
    assert prompt.count("## Current Task State") == 1
    assert "Current persona: implement" in prompt
    assert "Turn state: running" in prompt
    assert f"Goal: {goal}" in prompt
    assert "Workflow route: tdd -> verify" in prompt
    assert "Active workflows: tdd" in prompt
    assert "Todo: 0/1 done · 1 active · 0 pending" in prompt
    assert f"active sync: {todo_content}" in prompt
    assert "old goal" not in prompt
    assert "old trigger" not in prompt


@pytest.mark.asyncio
async def test_tool_update_is_visible_on_next_llm_call(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)
    graph = make_langgraph_execution(
        Config(
            model=ModelConfig(provider="openai", model="gpt-4o"),
            workspace=str(tmp_path),
        ),
        api_key="test-key",
    )
    graph.model = TrackingStreamingModel()
    _install_old_builder(graph, tmp_path)

    class FakeTodoTool:
        id = "todo"
        description = "fake todo"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
            return ToolResult(
                output="todo updated",
                metadata={
                    "todo_summary": "0/1 done · 1 active · 0 pending",
                    "todo_items": [
                        {"id": "sync", "content": "tool loop refresh", "status": "active"},
                    ],
                    "total": 1,
                    "done": 0,
                    "active": 1,
                    "pending": 0,
                },
            )

    graph.tools.replace(
        "todo",
        FakeTodoTool(),
        "fake todo",
        {"type": "object", "properties": {}},
    )

    async def allow_all(tool_calls, **_kwargs):
        return tool_calls, []

    graph._authorize_tool_calls = allow_all
    parent = AIMessage(
        content="",
        tool_calls=[{
            "name": "todo",
            "args": {"todos": []},
            "id": "call_todo",
            "type": "tool_call",
        }],
    )
    before_tool_state = _task_state("tool loop goal", _todo_state("before tool"))
    tool_update = await graph._execute_tools({
        "messages": [parent],
        "workspace": str(tmp_path),
        "persona": "implement",
        "plan_mode": False,
        "interaction_mode": "auto",
        "turn_state": "running",
        "step_count": 1,
        "task_state": before_tool_state.model_dump(mode="json"),
    })

    await graph._call_llm({
        "messages": [HumanMessage(content="continue"), parent, *tool_update["messages"]],
        "step_count": 1,
        "persona": "implement",
        "turn_state": "running",
        "task_state": tool_update["task_state"],
        "todo_state": tool_update["todo_state"],
    })

    prompt = "\n".join(str(message.content) for message in graph.model.messages)
    _assert_latest_prompt(prompt, goal="tool loop goal", todo_content="tool loop refresh")


@pytest.mark.asyncio
async def test_provider_overflow_retry_keeps_latest_task_state(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    class OverflowOnceModel(TrackingStreamingModel):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0
            self.messages_by_call = []

        async def astream(self, messages):
            self.calls += 1
            self.messages_by_call.append(list(messages))
            if self.calls == 1:
                if False:
                    yield AIMessageChunk(content="")
                error = RuntimeError("context length exceeded")
                error.status_code = 400
                raise error
            async for chunk in super().astream(messages):
                yield chunk

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)
    graph = make_langgraph_execution(
        Config(
            model=ModelConfig(provider="openai", model="gpt-4o"),
            workspace=str(tmp_path),
        ),
        api_key="test-key",
    )
    graph.model = OverflowOnceModel()
    graph._compaction.is_overflow = lambda _tokens: False
    _install_old_builder(graph, tmp_path)

    async def failed_preflight(*_args, **_kwargs):
        return None, None

    graph._preflight_compact_if_needed = failed_preflight
    todo_state = _todo_state("overflow refresh")
    latest_task_state = _task_state("overflow goal", todo_state)

    await graph._call_llm({
        "messages": [HumanMessage(id="turn-overflow-refresh", content="continue")],
        "step_count": 1,
        "persona": "implement",
        "turn_state": "running",
        "task_state": latest_task_state.model_dump(mode="json"),
        "todo_state": todo_state.model_dump(mode="json"),
    })

    assert graph.model.calls == 2
    retry_prompt = "\n".join(
        str(message.content) for message in graph.model.messages_by_call[1]
    )
    _assert_latest_prompt(
        retry_prompt,
        goal="overflow goal",
        todo_content="overflow refresh",
    )


@pytest.mark.asyncio
async def test_child_run_retry_keeps_latest_task_state(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.llm_turn as graph_module

    monkeypatch.setattr(graph_module, "StreamingRenderer", FakeRenderer)

    async def no_save_context_frame(**_kwargs):
        return None

    monkeypatch.setattr(graph_module, "save_main_context_frame", no_save_context_frame)
    graph = make_langgraph_execution(
        Config(
            model=ModelConfig(provider="mimo", model="mimo-v2.5"),
            workspace=str(tmp_path),
        ),
        api_key="test-key",
    )
    graph._session = SimpleNamespace(id="session-task-state-child-refresh")
    _install_old_builder(graph, tmp_path)
    root_id = graph.agent_gateway.ensure_root(graph._session.id)
    release = asyncio.Event()
    child_run_id = ""

    class SpawnsChildAfterFirstRequest(RepairsMalformedToolCallStreamingModel):
        async def astream(self, messages):
            nonlocal child_run_id
            async for chunk in super().astream(messages):
                yield chunk
            if self.calls == 1:
                async def runner(_run_id: str) -> str:
                    await release.wait()
                    return "done"

                child = await graph.agent_gateway.spawn(
                    session_id=graph._session.id,
                    parent_run_id=root_id,
                    agent_name="voidx",
                    description="Goal: retry-visible child",
                    runner=runner,
                )
                child_run_id = child.run_id

    graph.model = SpawnsChildAfterFirstRequest()
    todo_state = _todo_state("child retry refresh")
    latest_task_state = _task_state("child retry goal", todo_state)

    try:
        result = await graph._call_llm({
            "messages": [HumanMessage(content="continue")],
            "step_count": 1,
            "persona": "implement",
            "turn_state": "running",
            "task_state": latest_task_state.model_dump(mode="json"),
            "todo_state": todo_state.model_dump(mode="json"),
        })

        assert result["messages"][0].content == "repaired answer"
        retry_prompt = "\n".join(
            str(message.content) for message in graph.model.messages_by_call[1]
        )
        _assert_latest_prompt(
            retry_prompt,
            goal="child retry goal",
            todo_content="child retry refresh",
        )
        assert f"{child_run_id} [running] Goal: retry-visible child" in retry_prompt
    finally:
        release.set()
        if child_run_id:
            await graph.agent_gateway.wait(
                requester_run_id=root_id,
                target_run_id=child_run_id,
                timeout=1,
            )
