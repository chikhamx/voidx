"""Regression tests for core graph behavior."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace


import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

import voidx.persistence.sqlite as store

from voidx.agent.application.agents import (
    AgentDef,
    child_agent_descriptions_for_llm,
    get_agent,
    get_visible_agents,
)
from voidx.agent.application.prompts import BASE_SYSTEM, PERSONA_MODEL, persona_prompt
from voidx.agent.infrastructure.langgraph.runtime.convergence import is_step_hint_message
from voidx.agent.infrastructure.langgraph.runtime.runtime import current_parent_tool_call_id
from voidx.agent.infrastructure.langgraph.runtime.runtime_guards import RuntimeGuardState, WallClockGuardState
from voidx.agent.infrastructure.langgraph.execution import LangGraphExecution
from tests.langgraph_execution import make_langgraph_execution
from voidx.agent.infrastructure.langgraph.execution import AGENT_RESULT_PREVIEW_CHARS, _agent_result_preview
from voidx.agent.infrastructure.message_rows import RowMessageCacheEntry
from voidx.agent.application.runtime_context import InteractionMode, RuntimeContextBuilder
from voidx.config import Config, Settings
from voidx.agent.domain.user_profile import UserProfile
from voidx.llm.compaction import CompactionSelection
from voidx.agent.application.instruction import InstructionService, WorkflowRuntimeContext
from voidx.agent.adapters.persistence.session_repository import (
    MessageRow,
    SessionInfo,
    create_session,
    delete_session,
    load_messages,
    save_message,
)
from voidx.presentation.adapters.persistence.transcript_snapshot import load_transcript
from voidx.tooling.adapters.permission.in_memory_state import create_permission_service as PermissionService
from voidx.agent.domain.task.state import GoalResolution, GoalSpec, IntentResolution, PlanResolution
from voidx.agent.domain.task.intent import TaskIntent
from voidx.skills.context import SKILL_TOOL_CONTEXT_MARKER
from voidx.agent.application.automation.workflow.context import WORKFLOW_CONTEXT_MARKER
from voidx.agent.application.automation.workflow.runtime import WorkflowRunState, WorkflowRunStatus
from voidx.agent.domain.task.state import TaskState, ToolStatePatch
from voidx.agent.domain.automation.workflow import WorkflowRoute
from voidx.tooling.domain.context import ToolExecutionContext as ToolContext
from voidx.tooling.domain.result import ToolResult
from voidx.agent.adapters.tools.subagent import AgentResultContract, AgentTool
from voidx.tooling.application.registry import ToolRegistry
from voidx.presentation.output.dock import BottomInputDock, set_dock
from voidx.presentation.output.events import DockEventConsumer, TurnStarted, ui_events


def _graph(tmp_path):
    cfg = Config(workspace=str(tmp_path))
    return make_langgraph_execution(cfg, api_key=None)


def _task_state_json(**kwargs):
    return TaskState(**kwargs).model_dump(mode="json")


def _edit_args(file_path: str) -> dict:
    return {
        "file_path": file_path,
        "edits": [{"operation": "replace", "lineno": 1, "prefix": "old", "suffix": "old", "new_string": "new"}],
    }


def _result_task_state(result: dict) -> TaskState:
    return TaskState.model_validate(result["task_state"])


def _child_goal_resolution(
    goal_type: str = "feature",
    *,
    desc: str = "Implement the feature",
    join: str = "tdd",
    leave: str = "verify",
) -> GoalResolution:
    return GoalResolution(
        intent=IntentResolution(type=TaskIntent.CODING),
        goal=GoalSpec(desc=desc),
        plan=PlanResolution(join=join, leave=leave),
    )


def _child_result_contract(schema_name: str = "implementation_result") -> AgentResultContract:
    result_format = (
        "verdict=PASS|FAIL|NEEDS_CHANGE, findings, risks, verification_notes, next_actions"
        if schema_name == "review_result"
        else "status, files_changed, tests_run, risks, followups"
    )
    return AgentResultContract(
        schema_name=schema_name,
        format=result_format,
    )


def _subagent_contract_kwargs(
    *,
    goal_type: str = "inspect",
    desc: str = "Inspect the workspace",
    join: str = "review",
    leave: str = "review",
    schema_name: str = "inspection_result",
) -> dict:
    return {
        "goal_resolution": _child_goal_resolution(goal_type, desc=desc, join=join, leave=leave),
        "result_contract": _child_result_contract(schema_name),
    }


@pytest.fixture(autouse=True)
def isolated_memory_store(tmp_path):
    if store._conn is not None:
        store._conn.close()
    store._conn = None
    store.DATA_DIR = tmp_path / ".voidx"
    yield
    if store._conn is not None:
        store._conn.close()
    store._conn = None


def _tree_nodes(root):
    nodes = [root]
    for child in root.children:
        nodes.extend(_tree_nodes(child))
    return nodes


@pytest.mark.asyncio
async def test_run_subagent_persists_assistant_messages_to_subagent_jsonl(tmp_path, monkeypatch):
    import voidx.agent.infrastructure.langgraph.runtime.subagent as subagent_module

    session = await create_session(workspace=str(tmp_path))

    class FakeModel:
        def bind_tools(self, _tool_defs):
            return self

    async def fake_stream_llm(_model, _messages, _renderer, _protocol, **kwargs):
        return AIMessage(content="child answer")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    try:
        output = await subagent_module.run_subagent(
            get_agent("voidx"),
            "Inspect child path",
            "test-key",
            Config(workspace=str(tmp_path)),
            runtime_persona="explore",
            **_subagent_contract_kwargs(desc="Inspect child path"),
            session_id=session.id,
            agent_id=3,
            debug=False,
        )

        path = store.DATA_DIR / "sessions" / session.id / "subagents" / "agent_3.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        assert output == "child answer"
        assert rows[-1]["type"] == "assistant_message"
        assert rows[-1]["agent_run_id"] == "agent_3"
        assert rows[-1]["step"] == 1
        assert rows[-1]["content_preview"] == "child answer"
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_run_subagent_persists_tool_results_to_subagent_jsonl(tmp_path, monkeypatch):
    import voidx.agent.infrastructure.langgraph.runtime.subagent as subagent_module

    session = await create_session(workspace=str(tmp_path))
    stream_calls: list[list] = []

    class FakeModel:
        def bind_tools(self, _tool_defs):
            return self

    class FakeToolRegistry:
        def filtered_copy(self, _allowed_ids):
            return self

        def ids(self):
            return ["read"]

        def serialize_definitions(self):
            return [{"name": "read", "description": "read", "input_schema": {}}]

        async def execute_tool(self, tid, _targs, _ctx):
            assert tid == "read"
            assert _ctx.session_id == session.id
            return ToolResult(output="file contents")

    async def fake_stream_llm(_model, messages, _renderer, _protocol, **kwargs):
        stream_calls.append(list(messages))
        if len(stream_calls) == 1:
            return AIMessage(
                content="",
                tool_calls=[{"name": "read", "args": {"file_path": "x.py"}, "id": "call_read", "type": "tool_call"}],
            )
        return AIMessage(content="done")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)
    monkeypatch.setattr(subagent_module, "ToolRegistry", FakeToolRegistry)

    try:
        output = await subagent_module.run_subagent(
            AgentDef(
                name="explore",
                description="test",
                when_to_use="test",
                can_write=False,
                can_delegate=False,
            ),
            "Inspect child path",
            "test-key",
            Config(workspace=str(tmp_path)),
            runtime_persona="explore",
            **_subagent_contract_kwargs(desc="Inspect child path"),
            session_id=session.id,
            agent_id=5,
            debug=False,
        )

        path = store.DATA_DIR / "sessions" / session.id / "subagents" / "agent_5.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        assert output == "done"
        assert any(row["type"] == "tool_result" and row["tool_call_id"] == "call_read" for row in rows)
        tool_row = next(row for row in rows if row["type"] == "tool_result")
        assert tool_row["tool_name"] == "read"
        assert tool_row["content"] == "file contents"
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_run_subagent_injects_failure_loop_guidance(tmp_path, monkeypatch):
    import voidx.agent.infrastructure.langgraph.runtime.subagent as subagent_module

    stream_calls: list[list] = []
    tool_calls = 0

    class FakeModel:
        def bind_tools(self, _tool_defs):
            return self

    class FakeToolRegistry:
        def filtered_copy(self, _allowed_ids):
            return self

        def ids(self):
            return ["read"]

        def serialize_definitions(self):
            return [{"name": "read", "description": "read", "input_schema": {}}]

        async def execute_tool(self, tid, targs, _ctx):
            nonlocal tool_calls
            tool_calls += 1
            assert tid == "read"
            return ToolResult(
                output=f"File not found: {targs['file_path']}",
                metadata={"error": True, "error_kind": "file_not_found"},
            )

    async def fake_stream_llm(_model, messages, _renderer, _protocol, **kwargs):
        stream_calls.append(list(messages))
        if len(stream_calls) <= 2:
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": "read",
                    "args": {"file_path": "missing.py"},
                    "id": f"call_read_{len(stream_calls)}",
                    "type": "tool_call",
                }],
            )
        assert any("failed twice" in str(message.content) for message in messages)
        return AIMessage(content="done")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)
    monkeypatch.setattr(subagent_module, "ToolRegistry", FakeToolRegistry)

    output = await subagent_module.run_subagent(
        AgentDef(
            name="explore",
            description="test",
            when_to_use="test",
            can_write=False,
            can_delegate=False,
        ),
        "Inspect child path",
        "test-key",
        Config(workspace=str(tmp_path)),
        runtime_persona="explore",
        **_subagent_contract_kwargs(desc="Inspect child path"),
        debug=False,
    )

    assert output == "done"
    assert tool_calls == 2


@pytest.mark.asyncio
async def test_run_subagent_terminates_after_no_progress_cycles(tmp_path, monkeypatch):
    import voidx.agent.infrastructure.langgraph.runtime.subagent as subagent_module

    stream_calls: list[list] = []
    executed_tools: list[str] = []

    class FakeModel:
        def bind_tools(self, _tool_defs):
            return self

    class FakeToolRegistry:
        def filtered_copy(self, _allowed_ids):
            return self

        def ids(self):
            return ["checkpoint", "todo"]

        def serialize_definitions(self):
            return [
                {"name": "checkpoint", "description": "checkpoint", "input_schema": {}},
                {"name": "todo", "description": "manage tasks", "input_schema": {"properties": {"op": {"type": "string"}}}},
            ]

        async def execute_tool(self, tid, _targs, _ctx):
            executed_tools.append(tid)
            return ToolResult(output=f"{tid} ok")

    async def fake_stream_llm(_model, messages, _renderer, _protocol, **kwargs):
        stream_calls.append(list(messages))
        if len(stream_calls) == 4:
            assert any("No meaningful progress" in str(message.content) for message in messages)
        if len(stream_calls) <= 5:
            tool_name = "checkpoint" if len(stream_calls) % 2 else "todo"
            tool_args = {} if tool_name == "checkpoint" else {"op": "read"}
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": tool_name,
                    "args": tool_args,
                    "id": f"call_{len(stream_calls)}",
                    "type": "tool_call",
                }],
            )
        return AIMessage(content="missed guard termination")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)
    monkeypatch.setattr(subagent_module, "ToolRegistry", FakeToolRegistry)

    output = await subagent_module.run_subagent(
        AgentDef(
            name="explore",
            description="test",
            when_to_use="test",
            can_write=False,
            can_delegate=False,
        ),
        "Inspect child path",
        "test-key",
        Config(workspace=str(tmp_path)),
        runtime_persona="explore",
        **_subagent_contract_kwargs(desc="Inspect child path"),
        debug=False,
    )

    assert "No meaningful progress" in output
    assert executed_tools == [
        "checkpoint",
        "todo",
        "checkpoint",
        "todo",
        "checkpoint",
    ]


