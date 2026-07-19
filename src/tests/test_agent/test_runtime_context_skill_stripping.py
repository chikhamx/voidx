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
from voidx.runtime.task_state import GoalSpec, TaskState, TodoRunState, WorkflowRoute
from voidx.config import Config, UserProfile
from voidx.skills.context import (
    SKILL_TOOL_CONTEXT_MARKER,
    SKILL_TOOL_CONTEXT_STRIPPED_MARKER,
)
from voidx.workflow.runtime import WorkflowActivationSource, WorkflowRunState, WorkflowRunStatus

def test_runtime_context_strips_historical_skill_tool_context(tmp_path):
    tool_output = (
        '{"confirmed_intent": "implement"}\n\n'
        f"{SKILL_TOOL_CONTEXT_MARKER}\nScope: current-turn\n\n"
        "## Skill: tdd\n"
        "Source: bundled\n"
        "Body-Hash: abc123\n\n"
        "Full skill body"
    )
    messages = [
        HumanMessage(content="old request"),
        ToolMessage(content=tool_output, tool_call_id="call_intent"),
        HumanMessage(content="current request"),
    ]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
    ).build()

    context.apply_to_messages(messages)

    historical_tool = next(message for message in messages if isinstance(message, ToolMessage))
    assert SKILL_TOOL_CONTEXT_STRIPPED_MARKER in historical_tool.content
    assert "tdd sha256=abc123 source=bundled" in historical_tool.content
    assert "Full skill body" not in historical_tool.content


def test_runtime_context_strips_tool_skill_context_before_latest_ai_message(tmp_path):
    tool_output = (
        '{"loaded": true}\n\n'
        f"{SKILL_TOOL_CONTEXT_MARKER}\nScope: current-turn\n\n"
        "## Skill: tdd\n"
        "Source: bundled\n"
        "Body-Hash: abc123\n\n"
        "Full skill body"
    )
    messages = [
        HumanMessage(content="current request"),
        ToolMessage(content=tool_output, tool_call_id="call_skills"),
        AIMessage(content="latest reply"),
    ]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
    ).build()

    context.apply_to_messages(messages)

    historical_tool = next(message for message in messages if isinstance(message, ToolMessage))
    latest_ai = next(message for message in messages if isinstance(message, AIMessage))
    assert SKILL_TOOL_CONTEXT_STRIPPED_MARKER in historical_tool.content
    assert "Full skill body" not in historical_tool.content
    assert latest_ai.content.startswith("VOIDX_RUNTIME_CONTEXT")


def test_runtime_context_preserves_latest_tool_skill_context(tmp_path):
    tool_output = (
        '{"loaded": true}\n\n'
        f"{SKILL_TOOL_CONTEXT_MARKER}\nScope: current-turn\n\n"
        "## Skill: tdd\n"
        "Source: bundled\n"
        "Body-Hash: abc123\n\n"
        "Full skill body"
    )
    messages = [
        HumanMessage(content="current request"),
        ToolMessage(content=tool_output, tool_call_id="call_latest"),
    ]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
    ).build()

    context.apply_to_messages(messages)

    latest_tool = next(message for message in messages if isinstance(message, ToolMessage))
    assert SKILL_TOOL_CONTEXT_MARKER in latest_tool.content
    assert "Full skill body" in latest_tool.content
    assert latest_tool.content.startswith("VOIDX_RUNTIME_CONTEXT")


def test_runtime_context_preserves_current_tool_skill_context_batch(tmp_path):
    first_tool_output = (
        '{"loaded": "first"}\n\n'
        f"{SKILL_TOOL_CONTEXT_MARKER}\nScope: current-turn\n\n"
        "## Skill: tdd\n"
        "Source: bundled\n"
        "Body-Hash: first\n\n"
        "First skill body"
    )
    second_tool_output = (
        '{"loaded": "second"}\n\n'
        f"{SKILL_TOOL_CONTEXT_MARKER}\nScope: current-turn\n\n"
        "## Skill: verify\n"
        "Source: bundled\n"
        "Body-Hash: second\n\n"
        "Second skill body"
    )
    messages = [
        HumanMessage(content="current request"),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "skill", "args": {}, "id": "call_first"},
                {"name": "skill", "args": {}, "id": "call_second"},
            ],
        ),
        ToolMessage(content=first_tool_output, tool_call_id="call_first"),
        ToolMessage(content=second_tool_output, tool_call_id="call_second"),
    ]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
    ).build()

    context.apply_to_messages(messages)

    tool_messages = [message for message in messages if isinstance(message, ToolMessage)]
    assert len(tool_messages) == 2
    assert SKILL_TOOL_CONTEXT_MARKER in tool_messages[0].content
    assert "First skill body" in tool_messages[0].content
    assert SKILL_TOOL_CONTEXT_MARKER in tool_messages[1].content
    assert "Second skill body" in tool_messages[1].content
    assert tool_messages[1].content.startswith("VOIDX_RUNTIME_CONTEXT")


def test_runtime_context_strips_multiple_historical_skill_tool_context_blocks(tmp_path):
    tool_output = (
        f"{SKILL_TOOL_CONTEXT_MARKER}\nScope: current-turn\n\n"
        "## Skill: tdd\n"
        "Source: bundled\n"
        "Body-Hash: first\n\n"
        "First body\n\n"
        f"{SKILL_TOOL_CONTEXT_MARKER}\nScope: current-turn\n\n"
        "## Skill: verify\n"
        "Source: bundled\n"
        "Body-Hash: second\n\n"
        "Second body"
    )
    messages = [
        HumanMessage(content="old request"),
        ToolMessage(content=tool_output, tool_call_id="call_skills"),
        HumanMessage(content="current request"),
    ]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
    ).build()

    context.apply_to_messages(messages)

    historical_tool = next(message for message in messages if isinstance(message, ToolMessage))
    assert historical_tool.content.count(SKILL_TOOL_CONTEXT_STRIPPED_MARKER) == 2
    assert "tdd sha256=first source=bundled" in historical_tool.content
    assert "verify sha256=second source=bundled" in historical_tool.content
    assert "First body" not in historical_tool.content
    assert "Second body" not in historical_tool.content


def test_runtime_context_does_not_restrip_already_stripped_skill_tool_context(tmp_path):
    stripped_output = (
        f"{SKILL_TOOL_CONTEXT_STRIPPED_MARKER}\n"
        "- tdd sha256=abc123 source=bundled"
    )
    messages = [
        HumanMessage(content="old request"),
        ToolMessage(content=stripped_output, tool_call_id="call_skills"),
        HumanMessage(content="current request"),
    ]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
    ).build()

    context.apply_to_messages(messages)

    historical_tool = next(message for message in messages if isinstance(message, ToolMessage))
    assert historical_tool.content == stripped_output


def test_task_context_reports_active_workflow_without_skill_bodies(tmp_path):
    messages = [HumanMessage(content="current request")]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
        active_workflow_summaries=["tdd (implement persona)"],
    ).build()

    context.apply_to_messages(messages)

    task_context = messages[-1].content
    assert "Active workflow nodes: tdd (implement persona)" in task_context
    assert "MUST brainstorm before implementation" not in task_context


def test_task_context_only_contains_active_workflow_summaries(tmp_path):
    messages = [HumanMessage(content="current request")]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
        active_workflow_summaries=["tdd (implement persona)"],
    ).build()

    context.apply_to_messages(messages)

    assert "Active workflow nodes: tdd (implement persona)" in messages[-1].content
    assert "Full TDD body" not in messages[-1].content


def test_runtime_context_preserves_current_turn_skill_tool_context(tmp_path):
    tool_output = (
        '{"confirmed_intent": "implement"}\n\n'
        f"{SKILL_TOOL_CONTEXT_MARKER}\nScope: current-turn\n\n"
        "## Skill: tdd\n"
        "Source: bundled\n"
        "Body-Hash: abc123\n\n"
        "Full skill body"
    )
    messages = [
        HumanMessage(content="current request"),
        AIMessage(content="", tool_calls=[{"name": "on_intent", "args": {}, "id": "call_intent"}]),
        ToolMessage(content=tool_output, tool_call_id="call_intent"),
    ]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
    ).build()

    context.apply_to_messages(messages)

    current_tool = next(message for message in messages if isinstance(message, ToolMessage))
    assert SKILL_TOOL_CONTEXT_MARKER in current_tool.content
    assert "Full skill body" in current_tool.content
    assert SKILL_TOOL_CONTEXT_STRIPPED_MARKER not in current_tool.content
