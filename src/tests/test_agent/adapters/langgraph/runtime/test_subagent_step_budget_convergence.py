"""Regression tests for core graph behavior."""
from voidx.agent.domain.automation.workflow_dag import DEFAULT_WORKFLOW_DAG

from tests.tool_registry import build_registry
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
)
from voidx.agent.application.prompts import BASE_SYSTEM, PERSONA_MODEL, persona_prompt
from voidx.agent.adapters.langgraph.runtime.convergence import is_step_hint_message
from voidx.agent.adapters.langgraph.runtime.runtime import current_parent_tool_call_id
from voidx.agent.adapters.langgraph.runtime.runtime_guards import RuntimeGuardState, WallClockGuardState
from voidx.agent.adapters.langgraph.execution import LangGraphExecution
from tests.langgraph_execution import make_langgraph_execution
from voidx.agent.adapters.langgraph.execution import AGENT_RESULT_PREVIEW_CHARS, _agent_result_preview
from voidx.agent.adapters.persistence.message_rows import RowMessageCacheEntry
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
from voidx.tooling.domain.capability import ToolCapability
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


def _child_result_contract(contract_type: str = "implementation_result") -> AgentResultContract:
    result_format = (
        "verdict=PASS|FAIL|NEEDS_CHANGE, findings, risks, verification_notes, next_actions"
        if contract_type == "review_result"
        else "status, files_changed, tests_run, risks, followups"
    )
    return AgentResultContract(format=result_format)


def _subagent_contract_kwargs(
    *,
    goal_type: str = "inspect",
    desc: str = "Inspect the workspace",
    join: str = "review",
    leave: str = "review",
    contract_type: str = "inspection_result",
) -> dict:
    return {
        "goal_resolution": _child_goal_resolution(goal_type, desc=desc, join=join, leave=leave),
        "result_contract": _child_result_contract(contract_type),
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
async def test_subagent_skill_context_matches_orchestrator(tmp_path, monkeypatch):
    from voidx.agent.application.agents import get_agent
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module

    captured: dict[str, list] = {}

    class FakeModel:
        def bind_tools(self, _tool_defs):
            return self

    async def fake_stream_llm(_model, messages, _renderer, _protocol, **kwargs):
        captured["messages"] = messages
        return AIMessage(content="done")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)
    workflow_context = await InstructionService(str(tmp_path)).workflow_context_for(
        workflow_dag=DEFAULT_WORKFLOW_DAG,
        goal_type="feature",
        scope="Implement the feature",
        workflow_start="tdd",
    )

    output = await subagent_module.run_subagent(
        get_agent("voidx"),
        "Implement the feature",
        "test-key",
        Config(workspace=str(tmp_path)),
        runtime_persona="implement",
        **_subagent_contract_kwargs(
            goal_type="feature",
            desc="Implement the feature",
            join="tdd",
            leave="verify",
            contract_type="implementation_result",
            
        ),
        workflow_runtime_context=workflow_context,
        debug=False,
    )

    assert output == "done"
    system_prompt = next(
        message.content
        for message in captured["messages"]
        if isinstance(message, SystemMessage)
    )
    task_messages = [
        message for message in captured["messages"]
        if isinstance(message, HumanMessage)
        and str(message.content).startswith("VOIDX_RUNTIME_CONTEXT")
        and "## Current Task State" in str(message.content)
    ]
    assert all(
        not (
            isinstance(message, HumanMessage)
            and str(message.content).startswith(WORKFLOW_CONTEXT_MARKER)
        )
        for message in captured["messages"]
    )
    assert len(task_messages) == 1
    assert "Workflow Node: tdd" in system_prompt
    assert "Workflow Node: tdd" not in task_messages[0].content
    assert "Active workflow nodes: tdd" in task_messages[0].content


@pytest.mark.asyncio
async def test_subagent_inherits_parent_mcp_gateway(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module

    captured: dict[str, list] = {}

    class FakeModel:
        def bind_tools(self, tool_defs):
            captured["tool_defs"] = tool_defs
            return self

    async def fake_stream_llm(_model, _messages, _renderer, _protocol, **kwargs):
        return AIMessage(content="done")

    parent_tools = build_registry()
    parent_tools.replace(
        "mcp",
        object(),
        "MCP gateway",
        {"type": "object", "properties": {}},
    )

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    output = await subagent_module.run_subagent(
        AgentDef(
            name="explore",
            description="test",
            when_to_use="test",
            can_write=False,
            can_delegate=False,
        ),
        "Inspect the workspace",
        "test-key",
        Config(workspace=str(tmp_path)),
        **_subagent_contract_kwargs(),
        parent_tools=parent_tools,
        debug=False,
    )

    assert output == "done"
    tool_names = [tool["function"]["name"] for tool in captured["tool_defs"]]
    assert "mcp" in tool_names

@pytest.mark.asyncio
async def test_subagent_with_mcp_gateway_copies_parent_gateway(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module

    captured: dict[str, list] = {}
    calls: list[dict] = []

    class FakeModel:
        def bind_tools(self, tool_defs):
            captured["tool_defs"] = tool_defs
            return self

    class FakeMcpTool:
        async def execute(self, args, _ctx):
            calls.append(args)
            return ToolResult(output="mcp result")

    stream_count = 0

    async def fake_stream_llm(_model, _messages, _renderer, _protocol, **kwargs):
        nonlocal stream_count
        stream_count += 1
        if stream_count == 1:
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": "mcp",
                    "args": {
                        "op": "call",
                        "server": "demo",
                        "tool": "send_message",
                        "arguments": {"text": "hello"},
                    },
                    "id": "mcp1",
                    "type": "tool_call",
                }],
            )
        return AIMessage(content="done")

    parent_tools = build_registry()
    parent_tools.replace(
        "mcp",
        FakeMcpTool(),
        "MCP gateway",
        {"type": "object", "properties": {"op": {"type": "string"}, "arguments": {"type": "object"}}},
    )

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    output = await subagent_module.run_subagent(
        AgentDef(
            name="explore",
            description="test",
            when_to_use="test",
            can_write=False,
            can_delegate=False,
        ),
        "Send the message",
        "test-key",
        Config(workspace=str(tmp_path)),
        **_subagent_contract_kwargs(desc="Send the message"),
        parent_tools=parent_tools,
        debug=False,
    )

    assert output == "done"
    tool_names = [tool["function"]["name"] for tool in captured["tool_defs"]]
    assert "mcp" in tool_names
    assert calls == [{
        "op": "call",
        "server": "demo",
        "tool": "send_message",
        "arguments": {"text": "hello"},
    }]


@pytest.mark.asyncio
async def test_subagent_tool_filter_always_blocks_nested_agent_tool(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module

    captured: list[list[str]] = []

    class FakeModel:
        def bind_tools(self, tool_defs):
            captured.append([tool["function"]["name"] for tool in tool_defs])
            return self

    async def fake_stream_llm(_model, _messages, _renderer, _protocol, **kwargs):
        return AIMessage(content="done")

    parent_tools = build_registry()
    parent_tools.register(
        "agent",
        object(),
        "Agent demo",
        {"type": "object", "properties": {}},
        capability=ToolCapability.ORCHESTRATION,
    )

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    for can_delegate in (False, True):
        output = await subagent_module.run_subagent(
            AgentDef(
                name="explore",
                description="test",
                when_to_use="test",
                can_write=False,
                can_delegate=can_delegate,
            ),
            "Inspect the workspace",
            "test-key",
            Config(workspace=str(tmp_path)),
            **_subagent_contract_kwargs(),
            parent_tools=parent_tools,
            debug=False,
        )
        assert output == "done"

    assert "agent" not in captured[0]

