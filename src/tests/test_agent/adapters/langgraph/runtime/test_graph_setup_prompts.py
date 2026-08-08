"""Regression tests for core graph behavior."""

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


def test_graph_registers_agent_tool_not_task_tool(tmp_path):
    graph = _graph(tmp_path)
    ids = graph.tools.ids()

    assert "agent" in ids
    assert "agent_parallel" not in ids
    assert "on_intent" not in ids
    assert "clarify" in ids
    assert "checkpoint" in ids
    assert "skill" in ids
    assert "task" not in ids


def test_agent_parallel_tool_not_registered_when_disabled(tmp_path):
    graph = _graph(tmp_path)

    assert "agent_parallel" not in graph.tools.ids()


def test_persona_prompt_does_not_render_child_agent_scheduling():
    prompt = persona_prompt()

    assert "## Child-Agent Scheduling" not in prompt
    assert "Delegate at most one child agent in a response" not in prompt
    assert "multiple `agent` tool calls" not in prompt


def test_agent_tool_description_owns_delegation_gate():
    prompt = BASE_SYSTEM.render()
    tool_description = AgentTool(runner=None).description
    schema = AgentTool(runner=None).parameters_schema()

    assert (
        "when either it needs an independent context or it can proceed in parallel with the parent "
        "without overlapping the parent's active scope"
    ) in tool_description
    assert "does not inherit the caller's conversation history" in tool_description
    assert "### Delegation Rules" not in prompt
    assert "Delegate only independent parallel work" not in prompt
    assert {"mode", "goal", "detail", "scope"}.issubset(set(schema["required"]))
    assert "goal_resolution" not in schema["required"]
    assert "result" not in schema["required"]


def test_orchestrator_prompt_matches_agent_workflow_schema():
    child_descriptions = child_agent_descriptions_for_llm()
    tool_description = AgentTool(runner=None).description
    schema = AgentTool(runner=None).parameters_schema()

    # Tool description is concise; parameter requirements are in the schema.
    assert "well-scoped subtask" in tool_description
    assert {"mode", "goal", "detail", "scope"}.issubset(set(schema["required"]))
    assert "success_criteria" not in schema["properties"]
    assert "result_preset" not in schema["properties"]
    assert "persona" not in child_descriptions
    assert "requested runtime persona" not in child_descriptions


def test_voidx_persona_prompt_declares_core_rules():
    assert "Use active workflow gates as completion and transition criteria." in BASE_SYSTEM.render()
    assert "Subagents do not interact with the user" not in BASE_SYSTEM.render()
    assert "Switch persona" not in PERSONA_MODEL.render()
    assert "implement persona" not in PERSONA_MODEL.render()


def test_base_system_prompt_registers_all_runtime_personas():
    for persona in ("coordinate", "explore", "plan", "implement", "review"):
        assert f"**{persona}**" in PERSONA_MODEL.render()


def test_graph_session_date_uses_session_creation_date(tmp_path):
    session = SessionInfo(
        id="s1",
        workspace=str(tmp_path),
        created_at="2026-06-06T12:00:00",
        updated_at="2026-06-07T12:00:00",
    )

    graph = make_langgraph_execution(Config(workspace=str(tmp_path)), api_key=None, session=session)

    assert graph._session_date.startswith("2026-06-06 ")


def test_orchestrator_has_direct_edit_tools():
    from voidx.tooling.application.registry import ToolRegistry

    agent = get_agent("voidx")
    registry = build_registry()
    tool_ids = set(registry.ids())
    assert {"manage", "write", "replace"}.issubset(tool_ids)
    assert "file" not in tool_ids
    assert "line" not in tool_ids

    assert agent is not None
    assert "insert" not in tool_ids
    assert "edit" not in tool_ids
    assert "delete" not in tool_ids
    assert {"clarify", "checkpoint", "skill"}.issubset(tool_ids)
    assert get_agent("sub-voidx") is None
    assert get_agent("explore") is None
    assert get_agent("plan") is None
    assert get_agent("implement") is None
    assert get_agent("review") is None
    assert get_visible_agents() == [agent]
    assert "skill" in tool_ids
    assert agent.can_write is True
def test_agent_def_no_longer_renders_tool_contract():
    agent = get_agent("voidx")

    assert agent is not None
    assert not hasattr(agent, "tool_contract")


def test_agent_def_no_longer_owns_persona_prompt():
    agent = AgentDef(
        name="orchesrator",
        description="typo",
        when_to_use="never",
        can_write=False,
        can_delegate=False,
    )

    assert not hasattr(agent, "persona_prompt")


def test_brainstorm_workflow_does_not_write_design():
    from voidx.agent.domain.automation.workflow_dag import DEFAULT_WORKFLOW_DAG

    node = DEFAULT_WORKFLOW_DAG.nodes["brainstorm"]
    actions = [step.action for step in node.workflow]
    descriptions = [step.description for step in node.workflow]

    assert "Write design doc" not in actions
    assert not any("docs/specs" in description for description in descriptions)


def test_internal_title_and_compaction_are_not_registered_agents():
    assert get_agent("compaction") is None
    assert get_agent("title") is None
