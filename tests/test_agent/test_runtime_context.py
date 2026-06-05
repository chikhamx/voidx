import sys
from pathlib import Path
from typing import get_origin, get_type_hints

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from typing_extensions import NotRequired

from voidx.agent.runtime_context import (
    InteractionMode,
    RuntimeContextBuilder,
    TaskIntent,
    infer_task_intent,
)
from voidx.agent.state import AgentState
from voidx.agent.task_state import PendingApproval
from voidx.config import Config
from voidx.skills.runtime import SkillActivationSource, SkillRunState, SkillRunStatus


def test_agent_state_marks_runtime_turn_metadata_required():
    hints = get_type_hints(AgentState, include_extras=True)
    assert {
        key
        for key in (
            "interaction_mode",
            "task_intent",
            "intent_resolution_reason",
            "goal",
            "goal_phase",
            "goal_status",
            "goal_turn_count",
        )
        if get_origin(hints[key]) is NotRequired
    } == set()
    assert get_origin(hints["user_message_id"]) is NotRequired


def test_runtime_context_section_order_places_active_skills_before_task_state(tmp_path):
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        agent_prompt="You are voidx.",
        agent="orchestrator",
        role_prompt="Coordinate work.",
        mode_prompt="Plan mode is active.",
        tool_contract="- Available tools: read, agent",
        interaction_mode="goal",
        instructions=["Instructions from: AGENTS.md\nFollow project rules."],
        skill_instructions=["Skill instructions from: docs\nSkill: docs"],
        summary="Previous summary.",
        current_user_text="Review auth.py",
    ).build()

    assert context.section_names() == [
        "Base System",
        "Role Prompt",
        "Mode Prompt",
        "Tool Contract",
        "Workspace Facts",
        "Project Facts",
        "Long Summary",
        "Current Date",
        "Runtime State",
        "Recent Messages",
        "Active Skills",
        "Current Task State",
    ]

    system = context.render_system()
    assert system.index("## Role Prompt") < system.index("## Mode Prompt")
    assert system.index("## Mode Prompt") < system.index("## Tool Contract")
    assert system.index("## Tool Contract") < system.index("## Workspace Facts")
    assert system.index("## Current Date") < system.index("## Runtime State")


def test_runtime_context_applies_task_context_before_current_user(tmp_path):
    messages = [
        HumanMessage(content="old question"),
        AIMessage(content="old answer"),
        HumanMessage(content="current request"),
    ]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        agent_prompt="You are voidx.",
        agent="orchestrator",
        interaction_mode=InteractionMode.PLAN,
        skill_instructions=["Skill instructions from: docs\nSkill: docs"],
        current_user_text="current request",
    ).build()

    context.apply_to_messages(messages)

    assert isinstance(messages[0], SystemMessage)
    assert all(not isinstance(message, SystemMessage) for message in messages[1:])
    assert isinstance(messages[-1], HumanMessage)
    assert "Active Skills" in messages[-1].content
    assert "Current Task State" in messages[-1].content
    assert "## User Message" in messages[-1].content
    assert isinstance(messages[-1], HumanMessage)
    assert messages[-1].content.endswith("current request")


def test_runtime_context_preserves_multimodal_user_message_without_extra_system(tmp_path):
    messages = [
        HumanMessage(content=[
            {"type": "text", "text": "describe image"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
        ]),
    ]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        agent_prompt="You are voidx.",
        agent="orchestrator",
        interaction_mode=InteractionMode.GOAL,
        skill_instructions=["Skill instructions from: docs\nSkill: docs"],
        current_user_text="describe image",
    ).build()

    context.apply_to_messages(messages)

    assert isinstance(messages[0], SystemMessage)
    assert all(not isinstance(message, SystemMessage) for message in messages[1:])
    assert isinstance(messages[-1], HumanMessage)
    assert isinstance(messages[-1].content, list)
    assert messages[-1].content[0]["type"] == "text"
    assert "Active Skills" in messages[-1].content[0]["text"]
    assert messages[-1].content[1]["type"] == "text"
    assert messages[-1].content[2]["type"] == "image_url"


def test_interaction_mode_parse_is_safe():
    assert InteractionMode.parse("plan") == InteractionMode.PLAN
    assert InteractionMode.parse(None) == InteractionMode.AUTO
    assert InteractionMode.parse("auto") == InteractionMode.AUTO
    assert InteractionMode.parse("goal") == InteractionMode.GOAL
    assert InteractionMode.PLAN.denies_writes is True
    assert InteractionMode.GOAL.denies_writes is False
    with pytest.raises(ValueError):
        InteractionMode.parse("review")


def test_intent_classifier_does_not_treat_inspection_as_implementation():
    assert infer_task_intent("看看 voidx 的 agent 编排") == TaskIntent.INSPECT
    assert infer_task_intent("可以看看这个项目") == TaskIntent.INSPECT
    assert infer_task_intent("有什么更好的优化方案") == TaskIntent.DESIGN
    assert infer_task_intent("可以，直接改") == TaskIntent.IMPLEMENT
    assert infer_task_intent("对，可以") == TaskIntent.CHAT


def test_current_task_state_records_intent_and_implementation_gate(tmp_path):
    messages = [HumanMessage(content="看看这个项目")]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        agent_prompt="You are voidx.",
        agent="orchestrator",
        interaction_mode=InteractionMode.AUTO,
        current_user_text="看看这个项目",
    ).build()

    context.apply_to_messages(messages)

    assert "Intent: inspect" in messages[-1].content
    assert "Implementation intent explicit" not in messages[-1].content


def test_current_task_state_records_active_workflow_skills(tmp_path):
    messages = [HumanMessage(content="实现这个功能")]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        agent_prompt="You are voidx.",
        agent="implement",
        interaction_mode=InteractionMode.AUTO,
        current_user_text="实现这个功能",
        task_intent=TaskIntent.IMPLEMENT,
        active_skill_summaries=[
            "test-driven-development (implement role)",
            "verification-before-completion (implement lifecycle)",
        ],
    ).build()

    context.apply_to_messages(messages)

    assert "Active workflow skills: test-driven-development (implement role); verification-before-completion (implement lifecycle)" in messages[-1].content


def test_current_task_state_records_structured_skill_runs(tmp_path):
    messages = [HumanMessage(content="实现这个功能")]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        agent_prompt="You are voidx.",
        agent="implement",
        interaction_mode=InteractionMode.AUTO,
        current_user_text="实现这个功能",
        task_intent=TaskIntent.IMPLEMENT,
        skill_runs=[
            SkillRunState(
                name="test-driven-development",
                status=SkillRunStatus.ACTIVE,
                source=SkillActivationSource.WORKFLOW,
                reason="implement role",
                phase="implement",
                scope="实现这个功能",
                activated_turn=1,
                updated_turn=1,
            )
        ],
    ).build()

    context.apply_to_messages(messages)

    assert (
        "Skill run state: test-driven-development=active "
        "phase=implement source=workflow reason=implement role"
    ) in messages[-1].content


def test_current_task_state_records_refined_intent_and_visible_tools(tmp_path):
    messages = [HumanMessage(content="继续")]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        agent_prompt="You are voidx.",
        agent="orchestrator",
        interaction_mode=InteractionMode.AUTO,
        current_user_text="继续",
        task_intent=TaskIntent.IMPLEMENT,
        intent_refined=True,
        intent_source="on_intent",
        intent_confidence=0.91,
        available_tool_ids=["read", "edit"],
    ).build()

    context.apply_to_messages(messages)

    assert "Intent refined: true source=on_intent confidence=0.91" in messages[-1].content
    assert "Runtime-visible tools: read, edit" in messages[-1].content


def test_current_task_state_records_multiturn_approval_state(tmp_path):
    messages = [HumanMessage(content="给个方案")]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        agent_prompt="You are voidx.",
        agent="orchestrator",
        interaction_mode=InteractionMode.AUTO,
        current_user_text="给个方案",
        task_intent=TaskIntent.DESIGN,
        intent_resolution_reason="single-turn classifier matched design",
        pending_approval=PendingApproval(scope="优化 runtime context"),
    ).build()

    context.apply_to_messages(messages)

    assert "Intent: design" in messages[-1].content
    assert "Pending approval: implementation scope=优化 runtime context" in messages[-1].content
    assert "Suggestion: use plan_checkpoint" in messages[-1].content


def test_current_task_state_suggests_clarify_for_low_confidence_intent(tmp_path):
    messages = [HumanMessage(content="继续")]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        agent_prompt="You are voidx.",
        agent="orchestrator",
        interaction_mode=InteractionMode.AUTO,
        current_user_text="继续",
        task_intent=TaskIntent.AMBIGUOUS,
        intent_refined=True,
        intent_confidence=0.42,
    ).build()

    context.apply_to_messages(messages)

    assert "Suggestion: use clarify" in messages[-1].content


def test_current_task_state_records_goal_run(tmp_path):
    messages = [HumanMessage(content="给个方案")]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        agent_prompt="You are voidx.",
        agent="orchestrator",
        interaction_mode=InteractionMode.GOAL,
        current_user_text="给个方案",
        task_intent=TaskIntent.DESIGN,
        goal="优化 markdown 渲染截断",
        goal_phase="design",
        goal_status="active",
        goal_turn_count=1,
    ).build()

    context.apply_to_messages(messages)

    assert "Mode: goal" in messages[-1].content
    assert "Goal mode: true" in messages[-1].content
    assert "Goal: 优化 markdown 渲染截断" in messages[-1].content
    assert "Goal phase: design" in messages[-1].content
    assert "Goal status: active" in messages[-1].content
    assert "Goal turn count: 1" in messages[-1].content
