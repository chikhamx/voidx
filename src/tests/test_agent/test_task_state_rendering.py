import sys
from pathlib import Path
from typing import get_origin, get_type_hints


import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from typing_extensions import NotRequired

from voidx.agent.runtime_context import (
    COMPACTION_GUIDE_MARKER,
    ContextCompilerCache,
    InteractionMode,
    RuntimeContextBuilder,
    TaskIntent,
    is_goal_resolution_guide_content,
    raw_semantic_messages,
)
from voidx.agent.state import AgentState
from voidx.agent.task_state import GoalSpec, TaskState, TodoRunState, WorkflowRoute
from voidx.config import Config, UserProfile
from voidx.skills.context import (
    SKILL_TOOL_CONTEXT_MARKER,
    SKILL_TOOL_CONTEXT_STRIPPED_MARKER,
)
from voidx.workflow.runtime import WorkflowActivationSource, WorkflowRunState, WorkflowRunStatus

def test_interaction_mode_parse_is_safe():
    assert InteractionMode.parse("plan") == InteractionMode.PLAN
    assert InteractionMode.parse(None) == InteractionMode.AUTO
    assert InteractionMode.parse("auto") == InteractionMode.AUTO
    assert InteractionMode.parse("goal") == InteractionMode.GOAL
    assert InteractionMode.PLAN.denies_writes is True
    assert InteractionMode.GOAL.denies_writes is False
    with pytest.raises(ValueError):
        InteractionMode.parse("review")



def test_current_task_state_records_intent_and_implementation_gate(tmp_path):
    messages = [HumanMessage(content="看看这个项目")]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
    ).build()

    context.apply_to_messages(messages)

    assert "Intent: coding" in messages[-1].content
    assert "Implementation intent explicit" not in messages[-1].content


def test_current_task_state_records_active_workflow_nodes(tmp_path):
    messages = [HumanMessage(content="实现这个功能")]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="implement",
        interaction_mode=InteractionMode.AUTO,
        task_state=TaskState(current_intent=TaskIntent.CODING),
        active_workflow_summaries=[
            "tdd (implement persona)",
            "verify (implement lifecycle)",
        ],
    ).build()

    context.apply_to_messages(messages)

    assert "Active workflow nodes: tdd (implement persona); verify (implement lifecycle)" in messages[-1].content


def test_current_task_state_records_structured_workflow_runs(tmp_path):
    messages = [HumanMessage(content="实现这个功能")]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="implement",
        interaction_mode=InteractionMode.AUTO,
        task_state=TaskState(current_intent=TaskIntent.CODING),
        workflow_runs=[
            WorkflowRunState(
                name="tdd",
                status=WorkflowRunStatus.ACTIVE,
                source=WorkflowActivationSource.WORKFLOW,
                reason="implement persona",
                goal_type="feature",
                scope="实现这个功能",
            )
        ],
    ).build()

    context.apply_to_messages(messages)

    assert "Workflow run state:" not in messages[-1].content
    assert "Workflow exits [tdd]: implemented -> verify" in messages[-1].content
    assert "Workflow gate [tdd]" not in messages[-1].content
    assert "test written, red verified, implementation green" not in messages[-1].content


def test_current_task_state_records_workflow_route(tmp_path):
    messages = [HumanMessage(content="review 完并修复问题")]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="review",
        interaction_mode=InteractionMode.AUTO,
        task_state=TaskState(
            current_intent=TaskIntent.CODING,
            workflow_route=WorkflowRoute(join="review", leave="verify"),
        ),
    ).build()

    context.apply_to_messages(messages)

    assert "Workflow route: review -> verify" in messages[-1].content


def test_current_task_state_lists_feedback_design_and_plan_exits(tmp_path):
    messages = [HumanMessage(content="处理 review 反馈")]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="implement",
        interaction_mode=InteractionMode.AUTO,
        task_state=TaskState(current_intent=TaskIntent.CODING),
        workflow_runs=[
            WorkflowRunState(
                name="feedback",
                status=WorkflowRunStatus.ACTIVE,
                source=WorkflowActivationSource.TRANSITION,
                reason="review returned issues",
                goal_type="review",
                scope="review feedback",
            )
        ],
    ).build()

    context.apply_to_messages(messages)

    assert "Workflow exits [feedback]:" in messages[-1].content
    assert "needs_design -> brainstorm" in messages[-1].content
    assert "needs_plan -> plan" in messages[-1].content


def test_current_task_state_records_user_profile_preferences(tmp_path):
    messages = [HumanMessage(content="继续")]
    context = RuntimeContextBuilder(
        config=Config(
            workspace=str(tmp_path),
            user_profile=UserProfile(language="zh-CN", tone="direct"),
        ),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
        task_state=TaskState(current_intent=TaskIntent.CODING),
    ).build()

    context.apply_to_messages(messages)

    assert "Language instruction:" not in messages[0].content
    assert "Tone instruction: Be direct and practical. Lead with the answer or action." in messages[0].content
    assert "User language" not in messages[-1].content
    assert "User tone" not in messages[-1].content


def test_current_task_state_records_refined_intent_without_visible_tools(tmp_path):
    messages = [HumanMessage(content="继续")]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
        task_state=TaskState(current_intent=TaskIntent.CODING),
    ).build()

    context.apply_to_messages(messages)

    assert "Current persona: voidx" in messages[-1].content
    assert "Runtime-visible tools" not in messages[-1].content


def test_current_task_state_records_current_goal(tmp_path):
    messages = [HumanMessage(content="给个方案")]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
        task_state=TaskState(
            current_intent=TaskIntent.CODING,
            current_goal=GoalSpec(desc="优化 runtime context"),
        ),
    ).build()

    context.apply_to_messages(messages)

    assert "Intent: coding" in messages[-1].content
    assert "Goal: 优化 runtime context" in messages[-1].content
    assert "Pending approval" not in messages[-1].content


def test_current_task_state_records_goal_run(tmp_path):
    messages = [HumanMessage(content="给个方案")]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.GOAL,
        task_state=TaskState(
            current_intent=TaskIntent.CODING,
            current_goal=GoalSpec(desc="优化 markdown 渲染截断"),
        ),
    ).build()

    context.apply_to_messages(messages)

    assert "Current persona: voidx" in messages[-1].content
    assert "Goal: 优化 markdown 渲染截断" in messages[-1].content
