"""Regression tests for core graph behavior."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

import voidx.memory.store as store

from voidx.agent.agents import (
    AgentDef,
    child_agent_descriptions_for_llm,
    get_agent,
    get_visible_agents,
)
from voidx.agent.prompts import BASE_SYSTEM, PERSONA_MODEL, persona_prompt
from voidx.agent.graph.convergence import is_step_hint_message
from voidx.agent.graph.runtime import current_parent_tool_call_id
from voidx.agent.graph.runtime_guards import RuntimeGuardState, WallClockGuardState
from voidx.agent.graph import VoidXGraph
from voidx.agent.graph.tool_execution import AGENT_RESULT_PREVIEW_CHARS, _agent_result_preview
from voidx.agent.message_rows import RowMessageCacheEntry
from voidx.agent.runtime_context import InteractionMode, RuntimeContextBuilder
from voidx.config import Config, ParallelSubagentsConfig, Settings, UserProfile
from voidx.llm.compaction import CompactionSelection
from voidx.llm.instruction import InstructionService, WorkflowRuntimeContext
from voidx.memory.session import (
    MessageRow,
    SessionInfo,
    create_session,
    delete_session,
    load_messages,
    save_message,
)
from voidx.memory.transcript import load_transcript
from voidx.permission.service import PermissionService
from voidx.runtime import GoalResolution, GoalSpec, GoalType, IntentResolution, PlanResolution, TaskIntent
from voidx.skills.context import SKILL_TOOL_CONTEXT_MARKER
from voidx.workflow.context import WORKFLOW_CONTEXT_MARKER
from voidx.workflow.runtime import WorkflowRunState, WorkflowRunStatus
from voidx.agent.task_state import TaskState, ToolStatePatch, WorkflowRoute
from voidx.tools.base import ToolContext, ToolResult
from voidx.tools.agent import AgentResultContract, AgentTool
from voidx.tools.registry import ToolRegistry
from voidx.ui.output.dock import BottomInputDock, set_dock
from voidx.ui.output.events import DockEventConsumer, TurnStarted, ui_events


def _graph(tmp_path):
    cfg = Config(workspace=str(tmp_path))
    return VoidXGraph(cfg, api_key=None)


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
    goal_type: GoalType = GoalType.FEATURE,
    *,
    desc: str = "Implement the feature",
    join: str = "tdd",
    leave: str = "verify",
) -> GoalResolution:
    return GoalResolution(
        intent=IntentResolution(type=TaskIntent.CODING, desc="delegated child task"),
        goal=GoalSpec(type=goal_type, desc=desc),
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
    goal_type: GoalType = GoalType.INSPECT,
    desc: str = "Inspect the workspace",
    join: str = "review",
    leave: str = "review",
    schema_name: str = "inspection_result",
    step_budget: int = 4,
) -> dict:
    return {
        "goal_resolution": _child_goal_resolution(goal_type, desc=desc, join=join, leave=leave),
        "result_contract": _child_result_contract(schema_name),
        "step_budget": step_budget,
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


def test_agent_tool_description_hides_parallel_when_disabled(tmp_path):
    graph = _graph(tmp_path)
    agent_def = graph.tools.get_def("agent")

    assert agent_def is not None
    assert "run concurrently" not in agent_def.description
    assert "multiple `agent` tool calls" not in agent_def.description


def test_agent_tool_description_exposes_parallel_when_enabled(tmp_path):
    graph = VoidXGraph(
        Config(
            workspace=str(tmp_path),
            parallel_subagents=ParallelSubagentsConfig(enabled=True),
        ),
        api_key=None,
    )
    agent_def = graph.tools.get_def("agent")

    assert agent_def is not None
    assert "multiple `agent` tool calls" in agent_def.description
    assert "run concurrently" in agent_def.description


def test_orchestrator_prompt_mentions_delegation_gate():
    prompt = BASE_SYSTEM.render()
    schema = AgentTool(runner=None).parameters_schema()

    assert "Do not delegate single-file reads" in prompt
    assert "simple searches" in prompt
    assert "straightforward tasks you can do directly" in prompt
    assert {
        "mode",
        "task",
        "target",
    }.issubset(set(schema["required"]))
    assert "goal_resolution" not in schema["required"]
    assert "result" not in schema["required"]


def test_orchestrator_prompt_matches_agent_workflow_schema():
    child_descriptions = child_agent_descriptions_for_llm()
    tool_description = AgentTool(runner=None).description

    assert "mode, task, and one concrete target" in tool_description
    assert "success_criteria for implement and feedback" in tool_description
    assert "result_preset" in tool_description
    assert "persona" not in child_descriptions
    assert "requested runtime persona" not in child_descriptions


def test_voidx_persona_prompt_declares_core_rules():
    assert "workflow gate takes precedence over persona prompts" in BASE_SYSTEM.render()
    assert "Subagents do not interact with the user" in BASE_SYSTEM.render()
    assert "Switch persona" in PERSONA_MODEL.render()
    assert "implement persona" not in PERSONA_MODEL.render()


def test_base_system_prompt_registers_all_runtime_personas():
    for persona in ("coordinate", "explore", "plan", "implement", "review"):
        assert f"**{persona}**" in PERSONA_MODEL.render()


@pytest.mark.asyncio
async def test_clear_applies_saved_parallel_subagents_config(tmp_path):
    session = await create_session(
        workspace=str(tmp_path),
        provider="mimo",
        model="mimo-v2.5",
    )
    settings = Settings(str(tmp_path))
    settings.set_parallel_subagents(ParallelSubagentsConfig(enabled=True, max_concurrent=3))
    graph = VoidXGraph(
        Config(workspace=str(tmp_path)),
        api_key=None,
        session=session,
        settings=settings,
    )

    assert graph.config.parallel_subagents == ParallelSubagentsConfig()
    assert "multiple `agent` tool calls" not in graph.tools.get_def("agent").description

    await graph.clear_current_session()

    assert graph.config.parallel_subagents == ParallelSubagentsConfig(enabled=True, max_concurrent=3)
    agent_def = graph.tools.get_def("agent")
    assert agent_def is not None
    assert "multiple `agent` tool calls" in agent_def.description
    assert "run concurrently" in agent_def.description


@pytest.mark.asyncio
async def test_resume_applies_saved_parallel_subagents_config(tmp_path):
    session = await create_session(
        workspace=str(tmp_path),
        provider="mimo",
        model="mimo-v2.5",
    )
    settings = Settings(str(tmp_path))
    settings.set_parallel_subagents(ParallelSubagentsConfig(enabled=True, max_concurrent=3))
    graph = VoidXGraph(
        Config(workspace=str(tmp_path)),
        api_key=None,
        settings=settings,
    )

    assert graph.config.parallel_subagents == ParallelSubagentsConfig()
    assert "multiple `agent` tool calls" not in graph.tools.get_def("agent").description

    await graph.resume_session(session)

    assert graph.config.parallel_subagents == ParallelSubagentsConfig(enabled=True, max_concurrent=3)
    agent_def = graph.tools.get_def("agent")
    assert agent_def is not None
    assert "multiple `agent` tool calls" in agent_def.description
    assert "run concurrently" in agent_def.description


def test_graph_session_date_uses_session_creation_date(tmp_path):
    session = SessionInfo(
        id="s1",
        workspace=str(tmp_path),
        created_at="2026-06-06T12:00:00",
        updated_at="2026-06-07T12:00:00",
    )

    graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None, session=session)

    assert graph._session_date.startswith("2026-06-06 ")


def test_orchestrator_has_direct_edit_tools():
    agent = get_agent("voidx")

    assert agent is not None
    assert {"write", "insert", "replace", "edit"}.issubset(set(agent.tools))
    assert {"clarify", "checkpoint", "skill"}.issubset(set(agent.tools))
    assert get_agent("sub-voidx") is None
    assert get_agent("explore") is None
    assert get_agent("plan") is None
    assert get_agent("implement") is None
    assert get_agent("review") is None
    assert get_visible_agents() == [agent]
    assert "skill" in agent.tools
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
        tools=[],
        can_write=False,
        can_delegate=False,
    )

    assert not hasattr(agent, "persona_prompt")


def test_brainstorm_workflow_does_not_write_design():
    from voidx.workflow.dag import DEFAULT_WORKFLOW_DAG

    node = DEFAULT_WORKFLOW_DAG.nodes["brainstorm"]
    actions = [step.action for step in node.workflow]
    descriptions = [step.description for step in node.workflow]

    assert "Write design doc" not in actions
    assert not any("docs/specs" in description for description in descriptions)


def test_internal_title_and_compaction_are_not_registered_agents():
    assert get_agent("compaction") is None
    assert get_agent("title") is None

