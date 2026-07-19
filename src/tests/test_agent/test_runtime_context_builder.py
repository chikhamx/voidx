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

def _runtime_state_human_messages(messages):
    return [
        message
        for message in messages
        if isinstance(message, HumanMessage) and "## Runtime State" in str(message.content)
    ]


def test_agent_state_marks_runtime_turn_metadata_required():
    hints = get_type_hints(AgentState, include_extras=True)
    assert {
        key
        for key in (
            "interaction_mode",
            "task_state",
        )
        if key in hints and get_origin(hints[key]) is NotRequired
    } == {"task_state"}
    assert get_origin(hints["user_message_id"]) is NotRequired


def test_runtime_context_section_order_places_runtime_state_before_task_state(tmp_path):
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        workflow_runtime="Workflow definitions.",
        persona="voidx",
        persona_prompt="Coordinate work.",
        interaction_mode="goal",
        instructions=["Instructions from: AGENTS.md\nFollow project rules."],
        summary="Previous summary.",
    ).build()

    assert context.section_names() == [
        "Base System",
        "Persona",
        "Workflow Runtime",
        "Runtime State",
        "Project Instructions",
        "Session Time",
        "Long Summary",
        "Current Task State",
    ]

    assert "Active Skills" not in context.render_task_context()
    system = context.render_system()
    assert system.index("## Persona") < system.index("## Workflow Runtime")
    assert system.index("## Workflow Runtime") < system.index("## Runtime State")
    assert system.index("## Runtime State") < system.index("## Project Instructions")
    assert system.index("## Session Time") < system.index("## Long Summary")
    assert "## Mode" not in system
    assert "## Runtime Constraints" not in system
    assert "## Workspace Facts" not in system
    assert f"- Workspace: {tmp_path}" in system
    assert "- Platform:" in system
    assert "- Sandbox: workspace-write" in system
    assert "- Approval policy: untrusted" in system


def test_runtime_context_system_omits_stable_workflow_dag_overview(tmp_path):
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
    ).build()

    system = context.render_system()

    assert "## Workflow DAG" not in system
    assert "Workflow node definitions" not in system
    assert "## Workflow Node:" not in system
    assert "## Runtime Constraints" not in system
    assert "## Mode" not in system


def test_runtime_context_applies_task_context_before_current_user(tmp_path):
    messages = [
        HumanMessage(content="old question"),
        AIMessage(content="old answer"),
        HumanMessage(content="current request"),
        ToolMessage(content="tool result", tool_call_id="call_1"),
    ]
    task_state = TaskState(
        todo_state=TodoRunState.model_validate({
            "summary": "1/2 done · 1 active · 0 pending",
            "total": 2,
            "done": 1,
            "active": 1,
            "pending": 0,
            "active_items": [
                {"id": "ctx", "content": "update runtime context", "status": "active"},
            ],
            "items": [
                {"id": "done_item", "content": "finished", "status": "done"},
                {"id": "ctx", "content": "update runtime context", "status": "active"},
            ],
        })
    )
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.PLAN,
        task_state=task_state,
    ).build()

    context.apply_to_messages(messages)

    assert isinstance(messages[0], SystemMessage)
    assert "## Runtime State" in messages[0].content
    assert _runtime_state_human_messages(messages) == []
    assert all(not isinstance(message, SystemMessage) for message in messages[1:])
    assert isinstance(messages[-1], ToolMessage)
    assert messages[1].content == "old question"
    assert messages[3].content == "current request"
    assert "Active Skills" not in messages[-1].content
    assert "Current DateTime" not in messages[-1].content
    assert "## Runtime State" not in messages[-1].content
    assert "Current Task State" in messages[-1].content
    assert "Todo: 1/2 done · 1 active · 0 pending" in messages[-1].content
    assert "  - active ctx: update runtime context" in messages[-1].content
    assert "Active/Pending" not in messages[-1].content
    assert "Call todo with op=read" not in messages[-1].content
    assert "## Task Context" in messages[-1].content
    assert messages[-1].content.endswith("tool result")


def test_current_task_state_todo_omits_active_when_all_pending(tmp_path):
    task_state = TaskState(
        todo_state=TodoRunState.model_validate({
            "summary": "0/2 done · 0 active · 2 pending",
            "total": 2,
            "done": 0,
            "active": 0,
            "pending": 2,
            "active_items": [],
            "items": [
                {"id": "p1", "content": "first task", "status": "pending"},
                {"id": "p2", "content": "second task", "status": "pending"},
            ],
        })
    )
    messages = [HumanMessage(content="go")]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
        task_state=task_state,
    ).build()
    context.apply_to_messages(messages)
    assert "Todo: 0/2 done · 0 active · 2 pending" in messages[-1].content
    assert "  - pending p1: first task" in messages[-1].content
    assert "  - pending p2: second task" in messages[-1].content
    assert "active:" not in messages[-1].content


def test_current_task_state_todo_truncates_long_active_content(tmp_path):
    long_content = "x" * 100
    task_state = TaskState(
        todo_state=TodoRunState.model_validate({
            "summary": "0/1 done · 1 active · 0 pending",
            "total": 1,
            "done": 0,
            "active": 1,
            "pending": 0,
            "active_items": [{"id": "a1", "content": long_content, "status": "active"}],
            "items": [{"id": "a1", "content": long_content, "status": "active"}],
        })
    )
    messages = [HumanMessage(content="go")]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
        task_state=task_state,
    ).build()
    context.apply_to_messages(messages)
    assert "  - active a1: " + "x" * 80 + "…" in messages[-1].content
    assert "x" * 100 not in messages[-1].content


def test_current_task_state_todo_limits_visible_items(tmp_path):
    task_state = TaskState(
        todo_state=TodoRunState.model_validate({
            "summary": "0/5 done · 2 active · 3 pending",
            "total": 5,
            "done": 0,
            "active": 2,
            "pending": 3,
            "active_items": [],
            "items": [
                {"id": "a1", "content": "active one", "status": "active"},
                {"id": "a2", "content": "active two", "status": "active"},
                {"id": "p1", "content": "pending one", "status": "pending"},
                {"id": "p2", "content": "pending two", "status": "pending"},
                {"id": "p3", "content": "pending three", "status": "pending"},
            ],
        })
    )
    messages = [HumanMessage(content="go")]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
        task_state=task_state,
    ).build()

    context.apply_to_messages(messages)

    assert "  - active a1: active one" in messages[-1].content
    assert "  - active a2: active two" in messages[-1].content
    assert "  - pending p1: pending one" in messages[-1].content
    assert "pending p2" not in messages[-1].content
    assert "  - … 2 more active/pending todos" in messages[-1].content



def test_runtime_context_omits_goal_resolution_guide(tmp_path):
    messages = [
        HumanMessage(content="old question"),
        AIMessage(content="old answer"),
        HumanMessage(content="current request"),
    ]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
        task_state=TaskState(),
    ).build()

    context.apply_to_messages(messages)

    guide_indexes = [
        index
        for index, message in enumerate(messages)
        if isinstance(message, HumanMessage) and is_goal_resolution_guide_content(message.content)
    ]
    assert guide_indexes == []
    assert "Runtime State" in messages[0].content
    assert _runtime_state_human_messages(messages) == []
    assert messages[1].content == "old question"
    assert messages[2].content == "old answer"
    assert "Current Task State" in messages[3].content
    assert messages[3].content.endswith("current request")

    context.apply_to_messages(messages)

    assert sum(
        1
        for message in messages
        if isinstance(message, HumanMessage) and is_goal_resolution_guide_content(message.content)
    ) == 0


def test_runtime_context_omits_goal_resolution_guide_incrementally(tmp_path):
    cache = ContextCompilerCache()
    kwargs = dict(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
    )

    first, cache = RuntimeContextBuilder(**kwargs).build_incremental(cache)
    second, cache = RuntimeContextBuilder(**kwargs).build_incremental(cache)

    assert "Goal Resolution Guide" not in first.section_names()
    assert "Goal Resolution Guide" not in second.section_names()


def test_raw_semantic_messages_strips_compaction_guide_overlay():
    messages = [
        HumanMessage(content="old question"),
        HumanMessage(content=f"{COMPACTION_GUIDE_MARKER}\nScope: inline-context-compaction"),
        HumanMessage(content="current request"),
    ]

    raw = raw_semantic_messages(messages)

    assert [message.content for message in raw] == ["old question", "current request"]


def test_runtime_context_system_uses_session_date_not_runtime_state(tmp_path):
    first = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
        session_date="2026-06-06 CST",
        task_state=TaskState(current_intent=TaskIntent.GENERAL),
    ).build()
    second = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
        session_date="2026-06-06 CST",
        task_state=TaskState(current_intent=TaskIntent.CODING),
    ).build()

    assert first.render_system() == second.render_system()
    latest = [HumanMessage(content="second")]
    second.apply_to_messages(latest)
    assert "2026-06-06 10:02 CST" not in latest[0].content
    assert "Runtime State" in latest[0].content
    assert "Runtime State" not in latest[-1].content
    assert _runtime_state_human_messages(latest) == []
    assert "Current DateTime" not in latest[0].content


def test_runtime_context_builder_rejects_legacy_workflow_context_content(tmp_path):
    with pytest.raises(TypeError):
        RuntimeContextBuilder(
            config=Config(workspace=str(tmp_path)),
            workspace=str(tmp_path),
            base_system_prompt="You are voidx.",
            persona="voidx",
            interaction_mode=InteractionMode.AUTO,
            workflow_context_content="VOIDX_WORKFLOW_CONTEXT\nlegacy",
        )


def test_runtime_context_incremental_reuses_stable_system_message(tmp_path):
    cache = ContextCompilerCache()
    first, cache = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
        session_date="2026-06-06 CST",
    ).build_incremental(cache)
    second, cache = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
        session_date="2026-06-06 CST",
    ).build_incremental(cache)

    assert second.system_message is first.system_message
    assert second.render_system() == first.render_system()

def test_stable_prefix_rebuilds_on_summary_change(tmp_path):
    cache = ContextCompilerCache()
    first, cache = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
        session_date="2026-06-06 CST",
        summary="old summary",
    ).build_incremental(cache)
    second, cache = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
        session_date="2026-06-06 CST",
        summary="new summary",
    ).build_incremental(cache)

    assert second.system_message is not first.system_message
    assert "old summary" in first.render_system()
    assert "new summary" in second.render_system()


def test_runtime_context_recompile_does_not_duplicate_turn_overlay(tmp_path):
    messages = [HumanMessage(content="current request")]
    first = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
        session_date="2026-06-06 CST",
    ).build()
    second = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
        session_date="2026-06-06 CST",
    ).build()

    first.apply_to_messages(messages)
    second.apply_to_messages(messages)

    assert isinstance(messages[-1], HumanMessage)
    assert isinstance(messages[-1].content, str)
    assert messages[-1].content.count("VOIDX_RUNTIME_CONTEXT") == 1
    assert "2026-06-06 10:02 CST" not in messages[-1].content
    assert "2026-06-06 10:01 CST" not in messages[-1].content
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
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.GOAL,
    ).build()

    context.apply_to_messages(messages)

    assert isinstance(messages[0], SystemMessage)
    assert "## Runtime State" in messages[0].content
    assert _runtime_state_human_messages(messages) == []
    assert all(not isinstance(message, SystemMessage) for message in messages[1:])
    assert isinstance(messages[-1], HumanMessage)
    assert isinstance(messages[-1].content, list)
    assert messages[-1].content[0]["type"] == "text"
    assert "Current Task State" in messages[-1].content[0]["text"]
    assert "## Runtime State" not in messages[-1].content[0]["text"]
    assert "## Task Context" in messages[-1].content[0]["text"]
    assert "Current DateTime" not in messages[-1].content[0]["text"]
    assert "Active Skills" not in messages[-1].content[0]["text"]
    assert messages[-1].content[1]["type"] == "text"
    assert messages[-1].content[2]["type"] == "image_url"


def test_runtime_context_migrates_task_overlay_from_ai_message_to_latest_message(tmp_path):
    messages = [
        HumanMessage(content="current request"),
        AIMessage(content=(
            "VOIDX_RUNTIME_CONTEXT\n\n"
            "## Current Task State\n- Intent: coding\n\n"
            "## Task Context\nassistant visible text"
        )),
        ToolMessage(content="latest tool result", tool_call_id="call_latest"),
    ]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
    ).build()

    context.apply_to_messages(messages)

    assistant_message = next(message for message in messages if isinstance(message, AIMessage))
    latest_tool = next(message for message in messages if isinstance(message, ToolMessage))
    assert assistant_message.content == "assistant visible text"
    assert isinstance(latest_tool.content, str)
    assert latest_tool.content.startswith("VOIDX_RUNTIME_CONTEXT")
    assert latest_tool.content.endswith("latest tool result")


def test_runtime_context_migrates_task_overlay_from_ai_list_content_to_latest_message(tmp_path):
    messages = [
        HumanMessage(content="current request"),
        AIMessage(content=[
            {
                "type": "text",
                "text": (
                    "VOIDX_RUNTIME_CONTEXT\n\n"
                    "## Current Task State\n- Intent: coding\n\n"
                    "## Task Context\nassistant visible text"
                ),
            },
            {"type": "text", "text": "assistant second block"},
        ]),
        ToolMessage(content="latest tool result", tool_call_id="call_latest"),
    ]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
    ).build()

    context.apply_to_messages(messages)

    assistant_message = next(message for message in messages if isinstance(message, AIMessage))
    latest_tool = next(message for message in messages if isinstance(message, ToolMessage))
    assert assistant_message.content == [
        {"type": "text", "text": "assistant visible text"},
        {"type": "text", "text": "assistant second block"},
    ]
    assert isinstance(latest_tool.content, str)
    assert latest_tool.content.startswith("VOIDX_RUNTIME_CONTEXT")
    assert latest_tool.content.endswith("latest tool result")


def test_runtime_context_migrates_task_overlay_from_tool_message_to_latest_message(tmp_path):
    messages = [
        HumanMessage(content="current request"),
        ToolMessage(
            content=(
                "VOIDX_RUNTIME_CONTEXT\n\n"
                "## Current Task State\n- Intent: coding\n\n"
                "## Task Context\ntool visible text"
            ),
            tool_call_id="call_old",
        ),
        AIMessage(content="latest assistant reply"),
    ]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
    ).build()

    context.apply_to_messages(messages)

    old_tool = next(message for message in messages if isinstance(message, ToolMessage))
    latest_ai = next(message for message in messages if isinstance(message, AIMessage))
    assert old_tool.content == "tool visible text"
    assert old_tool.tool_call_id == "call_old"
    assert isinstance(latest_ai.content, str)
    assert latest_ai.content.startswith("VOIDX_RUNTIME_CONTEXT")
    assert latest_ai.content.endswith("latest assistant reply")



def test_apply_to_messages_does_not_trim_state(tmp_path):
    """apply_to_messages must not trim superseded reads from state (P1).
    Trimming happens only in the LLM request frame, not in state mutation."""
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
    ).build()

    numbered = lambda s, e: "\n".join(f"{i}\tline {i}" for i in range(s, e + 1))
    messages = [
        AIMessage(content="", tool_calls=[{"id": "old", "name": "read", "args": {"file_path": "f.py"}, "type": "tool_call"}]),
        ToolMessage(content=numbered(1, 100), tool_call_id="old", status="success"),
        AIMessage(content="", tool_calls=[{"id": "new", "name": "read", "args": {"file_path": "f.py"}, "type": "tool_call"}]),
        ToolMessage(content=numbered(1, 100), tool_call_id="new", status="success"),
    ]
    context.apply_to_messages(messages)
    tool_ids = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    assert "old" in tool_ids
    assert "new" in tool_ids
