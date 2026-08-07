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
from voidx.config import Config, Settings, UserProfile
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
async def test_prepare_does_not_auto_inject_project_skill_body(tmp_path):
    skill_dir = tmp_path / ".voidx" / "skills" / "docs"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: docs\ndescription: Documentation helper\n---\nWrite concise docs.",
        encoding="utf-8",
    )
    graph = make_langgraph_execution(
        Config(workspace=str(tmp_path)),
        api_key=None,
        settings=Settings(str(tmp_path)),
    )
    messages = [HumanMessage(content="Use $docs for this README")]
    state = {
        "messages": messages,
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
        "tool_results": {},
        "step_count": 0,
        "should_continue": True,
    }

    await graph._prepare_with_stream(state)

    assert isinstance(messages[0], SystemMessage)
    assert isinstance(messages[1], HumanMessage)
    assert "## Workflow Runtime" in messages[0].content
    assert "## Workflow Node:" in messages[0].content
    assert all(
        not (
            isinstance(message, HumanMessage)
            and str(message.content).startswith(WORKFLOW_CONTEXT_MARKER)
        )
        for message in messages
    )
    assert "Write concise docs." not in "\n".join(str(message.content) for message in messages)


@pytest.mark.asyncio
async def test_prepare_injects_workflow_nodes_from_task_state(tmp_path):
    graph = make_langgraph_execution(
        Config(workspace=str(tmp_path)),
        api_key=None,
        settings=Settings(str(tmp_path)),
    )
    messages = [HumanMessage(content="对，可以")]
    state = {
        "messages": messages,
        "workspace": str(tmp_path),
        "persona": "voidx",
        "plan_mode": False,
        "interaction_mode": "auto",
        "task_intent": "coding",
        "task_state": TaskState(
            current_intent=TaskIntent.CODING,
            current_goal=GoalSpec(desc="修复 runtime bug"),
            workflow_route=WorkflowRoute(join="debug", leave="verify"),
        ).model_dump(mode="json"),
        "tool_results": {},
        "step_count": 0,
        "should_continue": True,
    }

    result = await graph._prepare_with_stream(state)

    assert isinstance(messages[0], SystemMessage)
    assert "Workflow Node: debug" in messages[0].content
    assert "Workflow Node: tdd" in messages[0].content
    assert "Workflow Node: verify" in messages[0].content
    assert all(
        not (
            isinstance(message, HumanMessage)
            and str(message.content).startswith(WORKFLOW_CONTEXT_MARKER)
        )
        for message in messages
    )
    task_context_message = next(
        message
        for message in messages
        if isinstance(message, HumanMessage) and "Active workflow nodes: debug" in str(message.content)
    )
    result_task_state = TaskState.model_validate(result["task_state"])
    assert [name for name in (result_task_state.workflow_runs or {})] == ["debug"]
    assert "Workflow run state:" not in task_context_message.content


@pytest.mark.asyncio
async def test_prepare_syncs_triggered_workflow_to_status_state(tmp_path):
    graph = make_langgraph_execution(
        Config(workspace=str(tmp_path)),
        api_key=None,
        settings=Settings(str(tmp_path)),
    )
    invalidations = 0

    class FakeApp:
        def invalidate(self):
            nonlocal invalidations
            invalidations += 1

    graph._ui.bind_frontend( FakeApp())
    result = await graph._prepare_with_stream({
        "messages": [HumanMessage(content="debug this flaky test")],
        "workspace": str(tmp_path),
        "persona": "coordinate",
        "plan_mode": False,
        "interaction_mode": "auto",
        "task_state": TaskState(
            current_intent=TaskIntent.CODING,
            current_goal=GoalSpec(desc="debug this flaky test"),
            workflow_route=WorkflowRoute(join="debug", leave="verify"),
        ).model_dump(mode="json"),
        "tool_results": {},
        "step_count": 0,
        "should_continue": True,
    })

    result_task_state = TaskState.model_validate(result["task_state"])
    assert result_task_state.workflow_runs["debug"].status == WorkflowRunStatus.ACTIVE
    assert graph._task_state.workflow_runs["debug"].status == WorkflowRunStatus.ACTIVE
    assert invalidations > 0


@pytest.mark.asyncio
async def test_implement_subagent_injects_workflow_nodes(tmp_path, monkeypatch):
    from voidx.agent.application.agents import get_agent
    import voidx.agent.infrastructure.langgraph.runtime.subagent as subagent_module

    captured: dict[str, list] = {}

    class FakeModel:
        def bind_tools(self, tool_defs):
            captured["tool_ids"] = [tool.get("function", {}).get("name") for tool in tool_defs]
            return self

    async def fake_stream_llm(_model, messages, _renderer, _protocol, **kwargs):
        captured["messages"] = messages
        return AIMessage(content="done")

    monkeypatch.setattr(subagent_module, "create_chat_model", lambda *_args, **_kwargs: FakeModel())
    monkeypatch.setattr(subagent_module, "stream_llm", fake_stream_llm)
    workflow_context = await InstructionService(str(tmp_path)).workflow_context_for(
        goal_type="feature",
        scope="Implement the feature",
        workflow_start="tdd",
    )

    output = await subagent_module.run_subagent(
        get_agent("voidx"),
        "Implement the feature",
        "test-key",
        Config(
            workspace=str(tmp_path),
            user_profile=UserProfile(language="zh-CN", tone="direct"),
        ),
        runtime_persona="implement",
        **_subagent_contract_kwargs(
            goal_type="feature",
            desc="Implement the feature",
            join="tdd",
            leave="verify",
            schema_name="implementation_result",
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
    assert "## Persona\n## Persona Model" in system_prompt
    assert "## Workflow Runtime" in system_prompt
    assert "## Workflow Node:" in system_prompt
    assert "## Runtime Constraints" not in system_prompt
    assert "agent" not in captured["tool_ids"]
    assert "clarify" not in captured["tool_ids"]
    assert "checkpoint" not in captured["tool_ids"]
    assert "goal" not in captured["tool_ids"]
    assert "loop" not in captured["tool_ids"]
    task_payload = next(
        message.content
        for message in captured["messages"]
        if isinstance(message, HumanMessage)
        and "Implement the feature" in str(message.content)
    )
    assert "Child run constraints:" not in task_payload
    assert "Do not interact with the user directly." not in task_payload
    assert "Do not start another child agent." not in task_payload
    assert "Result contract:" in task_payload
    assert all(
        not (
            isinstance(message, HumanMessage)
            and str(message.content).startswith(WORKFLOW_CONTEXT_MARKER)
        )
        for message in captured["messages"]
    )
    runtime_state = system_prompt
    rendered_user = next(
        message.content
        for message in captured["messages"]
        if (
            isinstance(message, HumanMessage)
            and str(message.content).startswith("VOIDX_RUNTIME_CONTEXT")
            and "## Current Task State" in str(message.content)
        )
    )
    assert "Workflow Node: tdd" in system_prompt
    assert "Active workflow nodes: tdd" in rendered_user
    assert "**使用中文回复。**" in runtime_state
    assert "Prefer responding in Chinese (Simplified) unless the user explicitly asks otherwise." in runtime_state
    assert "Language instruction:" not in runtime_state
    assert "Tone instruction: Be direct and practical. Lead with the answer or action." in runtime_state
    assert all(
        not (isinstance(message, HumanMessage) and "## Runtime State" in str(message.content))
        for message in captured["messages"]
    )



@pytest.mark.asyncio
async def test_prepare_does_not_reactivate_satisfied_workflow_via_stale_route(tmp_path):
    """When a workflow tool advanced feedback->tdd, task_state.workflow_runs
    has feedback=SATISFIED and tdd=ACTIVE. But workflow_route.join still
    points at the old node (feedback) because workflow enter/advance does
    not update the route. The LLM prepare step must not use the stale
    route to reactivate feedback and overwrite its SATISFIED status."""
    graph = make_langgraph_execution(
        Config(workspace=str(tmp_path)),
        api_key=None,
        settings=Settings(str(tmp_path)),
    )
    result = await graph._prepare_with_stream({
        "messages": [HumanMessage(content="继续实现")],
        "workspace": str(tmp_path),
        "persona": "implement",
        "plan_mode": False,
        "interaction_mode": "auto",
        "task_state": TaskState(
            current_intent=TaskIntent.CODING,
            current_goal=GoalSpec(desc="实现 feature X"),
            workflow_route=WorkflowRoute(join="feedback", leave=None),
            workflow_runs={
                "feedback": WorkflowRunState(
                    name="feedback",
                    status=WorkflowRunStatus.SATISFIED,
                    reason="transition from feedback via nontrivial_fix",
                ),
                "tdd": WorkflowRunState(
                    name="tdd",
                    status=WorkflowRunStatus.ACTIVE,
                    reason="transition from feedback via nontrivial_fix",
                ),
            },
        ).model_dump(mode="json"),
        "tool_results": {},
        "step_count": 0,
        "should_continue": True,
    })

    result_task_state = TaskState.model_validate(result["task_state"])
    assert result_task_state.workflow_runs["tdd"].status == WorkflowRunStatus.ACTIVE
    assert result_task_state.workflow_runs["feedback"].status == WorkflowRunStatus.SATISFIED
