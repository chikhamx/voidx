"""Regression tests for core graph behavior."""
from voidx.agent.domain.automation.workflow_dag import DEFAULT_WORKFLOW_DAG

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
async def test_subagent_starts_from_isolated_task_context(tmp_path, monkeypatch):
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

    inherited_messages = [HumanMessage(content="Parent request")]
    RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
    ).build().apply_to_messages(inherited_messages)
    workflow_context = await InstructionService(str(tmp_path)).workflow_context_for(
        workflow_dag=DEFAULT_WORKFLOW_DAG,
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
            contract_type="review_result",
            
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
    assert "schema_name" not in task_payload
    assert "verdict=PASS|FAIL|NEEDS_CHANGE" in task_payload


@pytest.mark.asyncio
async def test_subagent_adds_last_tool_step_hint_to_payload_only(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module

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
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module

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
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module

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
            contract_type="inspection_result",
        ),
        debug=False,
    )

    assert output.startswith("summary:")
    contract_retry = captured_calls[2][-1]
    assert isinstance(contract_retry, HumanMessage)
    assert "Your previous response did not satisfy the child-agent result contract" in contract_retry.content
    assert "schema_name" not in contract_retry.content
    assert "format: status, files_changed, tests_run, risks, followups" in contract_retry.content


@pytest.mark.asyncio
async def test_subagent_contract_retry_exhausted_returns_contract_unsatisfied(tmp_path, monkeypatch):
    """When contract retries are exhausted, finish_reason must be contract_unsatisfied, not safety_limit."""
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module

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
            contract_type="inspection_result",
        ),
        debug=False,
    )

    assert run_metadata.get("finish_reason") == "contract_unsatisfied"
    assert "raw tool output" in output
    # 1 tool-call step + 1 initial summary + N retries, each producing a summary attempt
    assert len(captured_calls) == 2 + subagent_module._RESULT_CONTRACT_RETRY_LIMIT


@pytest.mark.asyncio
async def test_subagent_step_limit_runs_one_tool_free_final_call(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module
    from voidx.config import SubagentBudgetConfig
    from voidx.agent.adapters.langgraph.runtime.subagent_convergence import FINAL_CONVERGENCE_GUIDANCE

    calls: list[tuple[object, list]] = []
    executed_tools: list[str] = []
    sub_messages: list = []

    class BoundModel:
        pass

    class FakeModel:
        def __init__(self):
            self.bound = BoundModel()

        def bind_tools(self, _tool_defs):
            return self.bound

    model = FakeModel()

    class FakeToolRegistry:
        def filtered_copy(self, _allowed_ids):
            return self

        def ids(self):
            return ["read"]

        def serialize_definitions(self):
            return [{"name": "read", "description": "read", "input_schema": {}}]

        async def execute_tool(self, tool_id, _args, _ctx):
            executed_tools.append(tool_id)
            return ToolResult(output="finding from tool")

    async def fake_stream_llm(current_model, messages, _renderer, _protocol, **kwargs):
        calls.append((current_model, list(messages)))
        if len(calls) == 1:
            return AIMessage(
                content="finding before final",
                tool_calls=[{
                    "name": "read",
                    "args": {},
                    "id": "read-1",
                    "type": "tool_call",
                }],
            )
        return AIMessage(content="status: partial\nfindings: finding before final")

    monkeypatch.setattr(subagent_module, "ToolRegistry", FakeToolRegistry)
    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)

    run_metadata: dict[str, object] = {}
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
        Config(
            workspace=str(tmp_path),
            subagent_budget=SubagentBudgetConfig(step_limit=1),
        ),
        **_subagent_contract_kwargs(),
        sub_messages=sub_messages,
        run_metadata=run_metadata,
        debug=False,
    )

    assert output == "status: partial\nfindings: finding before final"
    assert executed_tools == ["read"]
    assert len(calls) == 2
    assert isinstance(calls[0][0], BoundModel)
    assert calls[1][0] is model
    assert FINAL_CONVERGENCE_GUIDANCE in calls[1][1][-1].content
    assert not any(
        isinstance(message, HumanMessage) and FINAL_CONVERGENCE_GUIDANCE in str(message.content)
        for message in sub_messages
    )
    assert run_metadata["finish_reason"] == "step_limit"


@pytest.mark.asyncio
async def test_subagent_context_soft_guidance_is_request_only(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module
    from voidx.config import SubagentBudgetConfig
    from voidx.agent.adapters.langgraph.runtime.subagent_convergence import SOFT_CONVERGENCE_GUIDANCE

    captured_messages: list = []
    sub_messages: list = []

    context_updates: list[int] = []
    saved_frames: list[dict] = []

    class FakeUsageStats:
        def update_context(self, tokens):
            context_updates.append(tokens)

        def record_call(self, *_args, **_kwargs):
            return None

    async def capture_frame(**kwargs):
        saved_frames.append(kwargs)

    class FakeModel:
        def bind_tools(self, _tool_defs):
            return self

    async def fake_stream_llm(_model, messages, _renderer, _protocol, **kwargs):
        captured_messages.extend(messages)
        return AIMessage(content="done")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)
    monkeypatch.setattr(
        subagent_module,
        "estimate_context_tokens_with_tools",
        lambda messages, *_args: (
            87
            if any(
                isinstance(message, HumanMessage)
                and SOFT_CONVERGENCE_GUIDANCE in str(message.content)
                for message in messages
            )
            else 80
        ),
    )
    monkeypatch.setattr(subagent_module, "save_context_frame_from_messages", capture_frame)

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
        Config(
            workspace=str(tmp_path),
            model={"context_window": 100},
            subagent_budget=SubagentBudgetConfig(
                context_soft_ratio=0.75,
                context_hard_ratio=0.9,
            ),
        ),
        **_subagent_contract_kwargs(),
        sub_messages=sub_messages,
        session_id="session-soft-accounting",
        usage_stats=FakeUsageStats(),
        debug=False,
    )

    assert output == "done"
    guidance = [
        message
        for message in captured_messages
        if isinstance(message, HumanMessage) and SOFT_CONVERGENCE_GUIDANCE in str(message.content)
    ]
    assert len(guidance) == 1
    assert guidance[0].additional_kwargs.get("_voidx_guidance") is True
    assert not any(
        isinstance(message, HumanMessage) and SOFT_CONVERGENCE_GUIDANCE in str(message.content)
        for message in sub_messages
    )
    assert context_updates == [87]
    assert len(saved_frames) == 1
    assert saved_frames[0]["messages"] == captured_messages
    assert saved_frames[0]["token_estimate"] == 87




@pytest.mark.asyncio
async def test_soft_guidance_that_crosses_context_hard_limit_uses_tool_free_final(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module
    from voidx.config import SubagentBudgetConfig
    from voidx.agent.adapters.langgraph.runtime.subagent_convergence import (
        FINAL_CONVERGENCE_GUIDANCE,
        SOFT_CONVERGENCE_GUIDANCE,
    )

    calls: list[tuple[object, list]] = []

    class BoundModel:
        pass

    class FakeModel:
        def __init__(self):
            self.bound = BoundModel()

        def bind_tools(self, _tool_defs):
            return self.bound

    model = FakeModel()

    async def fake_stream_llm(current_model, messages, _renderer, _protocol, **kwargs):
        calls.append((current_model, list(messages)))
        return AIMessage(content="status: partial\nfindings: context finding")

    def estimate(messages, *_args):
        if any(
            isinstance(message, HumanMessage)
            and SOFT_CONVERGENCE_GUIDANCE in str(message.content)
            for message in messages
        ):
            return 91
        return 89

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)
    monkeypatch.setattr(subagent_module, "estimate_context_tokens_with_tools", estimate)

    run_metadata: dict[str, object] = {}
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
        Config(
            workspace=str(tmp_path),
            model={"context_window": 100},
            subagent_budget=SubagentBudgetConfig(
                context_soft_ratio=0.75,
                context_hard_ratio=0.9,
            ),
        ),
        **_subagent_contract_kwargs(),
        run_metadata=run_metadata,
        debug=False,
    )

    assert output == "status: partial\nfindings: context finding"
    assert len(calls) == 1
    assert calls[0][0] is model
    assert FINAL_CONVERGENCE_GUIDANCE in calls[0][1][-1].content
    assert run_metadata["finish_reason"] == "context_limit"

@pytest.mark.asyncio
async def test_subagent_context_hard_limit_uses_tool_free_final_call(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module
    from voidx.config import SubagentBudgetConfig

    calls: list[object] = []

    class BoundModel:
        pass

    class FakeModel:
        def __init__(self):
            self.bound = BoundModel()

        def bind_tools(self, _tool_defs):
            return self.bound

    model = FakeModel()

    async def fake_stream_llm(current_model, _messages, _renderer, _protocol, **kwargs):
        calls.append(current_model)
        return AIMessage(content="status: partial\nfindings: context finding")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)
    monkeypatch.setattr(subagent_module, "estimate_context_tokens_with_tools", lambda *_args: 95)

    run_metadata: dict[str, object] = {}
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
        Config(
            workspace=str(tmp_path),
            model={"context_window": 100},
            subagent_budget=SubagentBudgetConfig(
                context_soft_ratio=0.75,
                context_hard_ratio=0.9,
            ),
        ),
        **_subagent_contract_kwargs(),
        run_metadata=run_metadata,
        debug=False,
    )

    assert output == "status: partial\nfindings: context finding"
    assert calls == [model]
    assert run_metadata["finish_reason"] == "context_limit"


@pytest.mark.asyncio
async def test_subagent_uses_chinese_convergence_guidance_for_chinese_profile(tmp_path, monkeypatch):
    import voidx.agent.adapters.langgraph.runtime.subagent as subagent_module
    from voidx.config import SubagentBudgetConfig
    from voidx.agent.domain.user_profile import UserProfile

    captured_messages: list = []

    class FakeModel:
        def bind_tools(self, _tool_defs):
            return self

    async def fake_stream_llm(_model, messages, _renderer, _protocol, **kwargs):
        captured_messages.extend(messages)
        return AIMessage(content="完成")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)
    monkeypatch.setattr(subagent_module, "estimate_context_tokens_with_tools", lambda *_args: 80)

    output = await subagent_module.run_subagent(
        AgentDef(
            name="explore",
            description="test",
            when_to_use="test",
            can_write=False,
            can_delegate=False,
        ),
        "检查工作区",
        "test-key",
        Config(
            workspace=str(tmp_path),
            model={"context_window": 100},
            user_profile=UserProfile(language="zh-CN"),
            subagent_budget=SubagentBudgetConfig(
                context_soft_ratio=0.75,
                context_hard_ratio=0.9,
            ),
        ),
        **_subagent_contract_kwargs(),
        debug=False,
    )

    contents = [str(message.content) for message in captured_messages]
    assert output == "完成"
    assert any("停止扩展范围" in content for content in contents)
    guidance_contents = [
        str(message.content)
        for message in captured_messages
        if message.additional_kwargs.get("_voidx_guidance")
    ]
    assert not any(
        "budget" in content.lower() or "context" in content.lower()
        for content in guidance_contents
    )
