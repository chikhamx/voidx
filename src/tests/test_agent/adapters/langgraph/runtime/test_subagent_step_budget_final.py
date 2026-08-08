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
async def test_subagent_starts_from_isolated_task_context(tmp_path, monkeypatch):
    import voidx.agent.infrastructure.langgraph.runtime.subagent as subagent_module

    captured: dict[str, list] = {}

    class FakeModel:
        def bind_tools(self, _tool_defs):
            return self

    async def fake_stream_llm(_model, messages, _renderer, _protocol, **kwargs):
        captured["messages"] = messages
        return AIMessage(content="done")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    inherited_messages = [HumanMessage(content="Parent request")]
    RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
    ).build().apply_to_messages(inherited_messages)
    workflow_context = await InstructionService(str(tmp_path)).workflow_context_for(
        goal_type="inspect",
        scope="Inspect the workspace",
    )

    output = await subagent_module.run_subagent(
        get_agent("voidx"),
        "Inspect the workspace",
        "test-key",
        Config(workspace=str(tmp_path)),
        runtime_persona="explore",
        **_subagent_contract_kwargs(),
        workflow_runtime_context=workflow_context,
        debug=False,
    )

    assert output == "done"
    system_prompt = next(
        message.content
        for message in captured["messages"]
        if isinstance(message, SystemMessage)
    )
    human_messages = [message for message in captured["messages"] if isinstance(message, HumanMessage)]
    workflow_context_messages = [
        message for message in human_messages
        if str(message.content).startswith(WORKFLOW_CONTEXT_MARKER)
    ]
    semantic_human_messages = [
        message for message in human_messages
        if not str(message.content).startswith(WORKFLOW_CONTEXT_MARKER)
        and not is_step_hint_message(message)
    ]
    assert workflow_context_messages == []
    assert "## Workflow Runtime" in system_prompt
    assert "## Workflow Node:" in system_prompt
    assert len(semantic_human_messages) == 1
    assert "Parent request" not in semantic_human_messages[0].content
    assert "Inspect the workspace" in semantic_human_messages[0].content
    assert "Current Task State" in semantic_human_messages[0].content


@pytest.mark.asyncio
async def test_subagent_injects_result_contract_into_task_payload(tmp_path, monkeypatch):
    import voidx.agent.infrastructure.langgraph.runtime.subagent as subagent_module

    captured: dict[str, list] = {}

    class FakeModel:
        def bind_tools(self, _tool_defs):
            return self

    async def fake_stream_llm(_model, messages, _renderer, _protocol, **kwargs):
        captured["messages"] = messages
        return AIMessage(content="done")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    output = await subagent_module.run_subagent(
        get_agent("voidx"),
        "Inspect the workspace",
        "test-key",
        Config(workspace=str(tmp_path)),
        runtime_persona="review",
        **_subagent_contract_kwargs(
            goal_type="review",
            desc="Inspect the workspace",
            join="review",
            leave="review",
            schema_name="review_result",
            
        ),
        debug=False,
    )

    assert output == "done"
    task_payload = next(
        message.content
        for message in captured["messages"]
        if isinstance(message, HumanMessage) and "Result contract" in str(message.content)
    )
    assert "Result contract" in task_payload
    assert "review_result" in task_payload
    assert "verdict=PASS|FAIL|NEEDS_CHANGE" in task_payload


@pytest.mark.asyncio
async def test_subagent_adds_last_tool_step_hint_to_payload_only(tmp_path, monkeypatch):
    import voidx.agent.infrastructure.langgraph.runtime.subagent as subagent_module

    captured: dict[str, list] = {}
    sub_messages: list = []

    class FakeModel:
        def bind_tools(self, _tool_defs):
            return self

    async def fake_stream_llm(_model, messages, _renderer, _protocol, **kwargs):
        captured["messages"] = messages
        return AIMessage(content="done")

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
        sub_messages=sub_messages,
        debug=False,
    )

    assert output == "done"
    assert not any(is_step_hint_message(message) for message in sub_messages)


@pytest.mark.asyncio
async def test_subagent_final_step_fallback_does_not_leak_hint_to_sub_messages(tmp_path, monkeypatch):
    import voidx.agent.infrastructure.langgraph.runtime.subagent as subagent_module

    captured_calls: list[list] = []
    sub_messages: list = []

    class FakeModel:
        def bind_tools(self, _tool_defs):
            return self

    class FakeToolRegistry:
        def filtered_copy(self, _allowed_ids):
            return self

        def ids(self):
            return ["read"]

        def serialize_definitions(self):
            return [{
                "type": "function",
                "function": {
                    "name": "fake_tool",
                    "description": "fake",
                    "parameters": {"type": "object", "properties": {}},
                    "strict": True,
                },
            }]

        async def execute_tool(self, _tool_id, _args, _ctx):
            return ToolResult(output="read src/voidx/agent/graph/subagent.py")

    async def fake_stream_llm(_model, messages, _renderer, _protocol, **kwargs):
        captured_calls.append(messages)
        if len(captured_calls) == 1:
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": "fake_tool",
                    "args": {},
                    "id": "tc1",
                    "type": "tool_call",
                }],
            )
        return AIMessage(content="")

    monkeypatch.setattr(subagent_module, "ToolRegistry", FakeToolRegistry)
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
        sub_messages=sub_messages,
        debug=False,
    )


    assert output == ""
    assert not any(is_step_hint_message(message) for message in sub_messages)


@pytest.mark.asyncio
async def test_subagent_requires_structured_contract_after_tool_work(tmp_path, monkeypatch):
    import voidx.agent.infrastructure.langgraph.runtime.subagent as subagent_module

    captured_calls: list[list] = []

    class FakeModel:
        def bind_tools(self, _tool_defs):
            return self

    class FakeToolRegistry:
        def filtered_copy(self, _allowed_ids):
            return self

        def ids(self):
            return ["search"]

        def serialize_definitions(self):
            return [{
                "type": "function",
                "function": {
                    "name": "search",
                    "description": "fake grep",
                    "parameters": {"type": "object", "properties": {}},
                    "strict": True,
                },
            }]

        async def execute_tool(self, _tool_id, _args, _ctx):
            return ToolResult(output="src/voidx/tools/websearch.py: def _parse_duckduckgo_html(html): ...")

    async def fake_stream_llm(_model, messages, _renderer, _protocol, **kwargs):
        captured_calls.append(messages)
        if len(captured_calls) == 1:
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": "search",
                    "args": {"pattern": "TaskTracker"},
                    "id": "tc1",
                    "type": "tool_call",
                }],
            )
        if len(captured_calls) == 2:
            return AIMessage(content="src/voidx/tools/websearch.py: def _parse_duckduckgo_html(html): ...")
        return AIMessage(content="summary: searched code\nevidence: websearch.py snippet\nfindings: no issue\nopen_questions: none")

    monkeypatch.setattr(subagent_module, "ToolRegistry", FakeToolRegistry)
    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    output = await subagent_module.run_subagent(
        AgentDef(
            name="review",
            description="test",
            when_to_use="test",
            can_write=False,
            can_delegate=False,
        ),
        "Review subagent behavior",
        "test-key",
        Config(workspace=str(tmp_path)),
        runtime_persona="review",
        **_subagent_contract_kwargs(
            goal_type="review",
            desc="Review subagent behavior",
            join="review",
            leave="review",
            schema_name="inspection_result",
        ),
        debug=False,
    )

    assert output.startswith("summary:")
    contract_retry = captured_calls[2][-1]
    assert isinstance(contract_retry, HumanMessage)
    assert "Your previous response did not satisfy the child-agent result contract" in contract_retry.content
    assert "schema_name: inspection_result" in contract_retry.content


@pytest.mark.asyncio
async def test_subagent_contract_retry_exhausted_returns_contract_unsatisfied(tmp_path, monkeypatch):
    """When contract retries are exhausted, finish_reason must be contract_unsatisfied, not safety_limit."""
    import voidx.agent.infrastructure.langgraph.runtime.subagent as subagent_module

    captured_calls: list[list] = []

    class FakeModel:
        def bind_tools(self, _tool_defs):
            return self

    class FakeToolRegistry:
        def filtered_copy(self, _allowed_ids):
            return self

        def ids(self):
            return ["search"]

        def serialize_definitions(self):
            return [{
                "type": "function",
                "function": {
                    "name": "search",
                    "description": "fake grep",
                    "parameters": {"type": "object", "properties": {}},
                    "strict": True,
                },
            }]

        async def execute_tool(self, _tool_id, _args, _ctx):
            return ToolResult(output="src/voidx/tools/websearch.py: def _parse_duckduckgo_html(html): ...")

    async def fake_stream_llm(_model, messages, _renderer, _protocol, **kwargs):
        captured_calls.append(messages)
        if len(captured_calls) == 1:
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": "search",
                    "args": {"pattern": "TaskTracker"},
                    "id": "tc1",
                    "type": "tool_call",
                }],
            )
        return AIMessage(content="raw tool output without contract fields")

    monkeypatch.setattr(subagent_module, "ToolRegistry", FakeToolRegistry)
    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    run_metadata: dict[str, object] = {}
    output = await subagent_module.run_subagent(
        AgentDef(
            name="review",
            description="test",
            when_to_use="test",
            can_write=False,
            can_delegate=False,
        ),
        "Review subagent behavior",
        "test-key",
        Config(workspace=str(tmp_path)),
        runtime_persona="review",
        run_metadata=run_metadata,
        **_subagent_contract_kwargs(
            goal_type="review",
            desc="Review subagent behavior",
            join="review",
            leave="review",
            schema_name="inspection_result",
        ),
        debug=False,
    )

    assert run_metadata.get("finish_reason") == "contract_unsatisfied"
    assert "raw tool output" in output
    # 1 tool-call step + 1 initial summary + N retries, each producing a summary attempt
    assert len(captured_calls) == 2 + subagent_module._RESULT_CONTRACT_RETRY_LIMIT
