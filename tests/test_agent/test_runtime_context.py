import sys
from pathlib import Path
from typing import get_origin, get_type_hints

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from typing_extensions import NotRequired

from voidx.agent.runtime_context import (
    COMPACTION_GUIDE_MARKER,
    ContextCompilerCache,
    InteractionMode,
    RuntimeContextBuilder,
    TaskIntent,
    infer_task_intent,
    is_goal_resolution_guide_content,
    raw_semantic_messages,
)
from voidx.agent.state import AgentState
from voidx.agent.task_state import GoalType, PendingApproval, TaskState, WorkflowRoute, goal_from_text
from voidx.config import Config, UserProfile
from voidx.skills.context import (
    SKILL_CONTEXT_MARKER,
    SKILL_CONTEXT_SCOPE,
    SKILL_TOOL_CONTEXT_MARKER,
    SKILL_TOOL_CONTEXT_STRIPPED_MARKER,
    render_skill_context,
)
from voidx.workflow.runtime import WorkflowActivationSource, WorkflowRunState, WorkflowRunStatus


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


def test_runtime_context_section_order_places_skill_context_before_task_state(tmp_path):
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        persona_prompt="Coordinate work.",
        mode_prompt="Plan mode is active.",
        tool_contract="- Available tools: read, agent",
        interaction_mode="goal",
        instructions=["Instructions from: AGENTS.md\nFollow project rules."],
        skill_context_content=render_skill_context(["Skill instructions from: docs\nSkill: docs"]),
        summary="Previous summary.",
        current_user_text="Review auth.py",
    ).build()

    assert context.section_names() == [
        "Base System",
        "Agent Role",
        "Mode",
        "Tool Contract",
        "Workspace Facts",
        "Project Facts",
        "Session Time",
        "Long Summary",
        "Skill Context",
        "Runtime State",
        "Current Task State",
    ]

    assert context.skill_context_content.startswith(SKILL_CONTEXT_MARKER)
    assert f"Scope: {SKILL_CONTEXT_SCOPE}" in context.skill_context_content
    assert "Treat inactive skill bodies as reference material only." in context.skill_context_content
    assert "Skill instructions from: docs" in context.skill_context_content
    assert "Active Skills" not in context.render_task_context()
    system = context.render_system()
    assert system.index("## Agent Role") < system.index("## Mode")
    assert system.index("## Mode") < system.index("## Tool Contract")
    assert system.index("## Tool Contract") < system.index("## Workspace Facts")
    assert system.index("## Session Time") < system.index("## Long Summary")
    assert "## Runtime State" not in system


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


def test_runtime_context_applies_task_context_before_current_user(tmp_path):
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
        interaction_mode=InteractionMode.PLAN,
        skill_context_content=render_skill_context(["Skill instructions from: docs\nSkill: docs"]),
        current_user_text="current request",
    ).build()

    context.apply_to_messages(messages)

    assert isinstance(messages[0], SystemMessage)
    assert isinstance(messages[1], HumanMessage)
    assert messages[1].content.startswith(SKILL_CONTEXT_MARKER)
    assert all(not isinstance(message, SystemMessage) for message in messages[1:])
    assert isinstance(messages[-1], HumanMessage)
    assert messages[2].content == "old question"
    assert "Active Skills" not in messages[-1].content
    assert "Runtime State" in messages[-1].content
    assert "Current DateTime" not in messages[-1].content
    assert "Current Task State" in messages[-1].content
    assert "## User Message" in messages[-1].content
    assert isinstance(messages[-1], HumanMessage)
    assert messages[-1].content.endswith("current request")


def test_runtime_context_inserts_goal_resolution_guide_before_current_user(tmp_path):
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
        current_user_text="current request",
        task_state=TaskState(recent_user_texts=["approved plan", "continue"]),
        include_goal_resolution_guide=True,
    ).build()

    context.apply_to_messages(messages)

    guide_indexes = [
        index
        for index, message in enumerate(messages)
        if isinstance(message, HumanMessage) and is_goal_resolution_guide_content(message.content)
    ]
    assert guide_indexes == [3]
    assert '"recent_user_texts": [\n    "approved plan",\n    "continue"\n  ]' in messages[3].content
    assert messages[2].content == "old answer"
    assert "Runtime State" in messages[4].content
    assert messages[4].content.endswith("current request")

    context.apply_to_messages(messages)

    assert sum(
        1
        for message in messages
        if isinstance(message, HumanMessage) and is_goal_resolution_guide_content(message.content)
    ) == 1


def test_runtime_context_reuses_goal_resolution_guide_message_incrementally(tmp_path):
    cache = ContextCompilerCache()
    kwargs = dict(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
        current_user_text="current request",
        include_goal_resolution_guide=True,
    )

    first, cache = RuntimeContextBuilder(**kwargs).build_incremental(cache)
    second, cache = RuntimeContextBuilder(**kwargs).build_incremental(cache)

    assert first.goal_resolution_guide_message is not None
    assert second.goal_resolution_guide_message is first.goal_resolution_guide_message


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
        current_user_text="first",
        task_state=TaskState(current_intent=TaskIntent.GENERAL),
    ).build()
    second = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
        session_date="2026-06-06 CST",
        current_user_text="second",
        task_state=TaskState(current_intent=TaskIntent.CODING),
    ).build()

    assert first.render_system() == second.render_system()
    latest = [HumanMessage(content="second")]
    second.apply_to_messages(latest)
    assert "2026-06-06 10:02 CST" not in latest[-1].content
    assert "Runtime State" in latest[-1].content
    assert "Current DateTime" not in latest[-1].content


def test_runtime_context_incremental_reuses_stable_system_message(tmp_path):
    cache = ContextCompilerCache()
    first, cache = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
        session_date="2026-06-06 CST",
        current_user_text="first",
    ).build_incremental(cache)
    second, cache = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
        session_date="2026-06-06 CST",
        current_user_text="second",
    ).build_incremental(cache)

    assert second.system_message is first.system_message
    assert second.render_system() == first.render_system()


def test_runtime_context_incremental_reuses_skill_context_message(tmp_path):
    cache = ContextCompilerCache()
    first, cache = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
        skill_context_content=f"{SKILL_CONTEXT_MARKER}\n\n## Skill: docs\nBody-Hash: abc\n\nDocs rules",
        current_user_text="first",
    ).build_incremental(cache)
    second, cache = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
        skill_context_content=f"{SKILL_CONTEXT_MARKER}\n\n## Skill: docs\nBody-Hash: abc\n\nDocs rules",
        current_user_text="second",
    ).build_incremental(cache)

    assert first.skill_context_message is not None
    assert second.skill_context_message is first.skill_context_message


def test_skill_context_cache_key_uses_sorted_name_and_body_hash(tmp_path):
    cache = ContextCompilerCache()
    first_content = (
        f"{SKILL_CONTEXT_MARKER}\n\n"
        "## Skill: alpha\nBody-Hash: aaa\n\nAlpha body\n\n"
        "## Skill: beta\nBody-Hash: bbb\n\nBeta body"
    )
    second_content = (
        f"{SKILL_CONTEXT_MARKER}\n\n"
        "## Skill: beta\nBody-Hash: bbb\n\nBeta body\n\n"
        "## Skill: alpha\nBody-Hash: aaa\n\nAlpha body"
    )
    first, cache = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
        skill_context_content=first_content,
        current_user_text="first",
    ).build_incremental(cache)
    second, cache = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
        skill_context_content=second_content,
        current_user_text="second",
    ).build_incremental(cache)

    assert first.skill_context_message is not None
    assert second.skill_context_message is first.skill_context_message


def test_skill_context_cache_rebuilds_when_body_hash_changes(tmp_path):
    cache = ContextCompilerCache()
    first, cache = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
        skill_context_content=f"{SKILL_CONTEXT_MARKER}\n\n## Skill: docs\nBody-Hash: old\n\nOld docs rules",
        current_user_text="first",
    ).build_incremental(cache)
    second, cache = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
        skill_context_content=f"{SKILL_CONTEXT_MARKER}\n\n## Skill: docs\nBody-Hash: new\n\nNew docs rules",
        current_user_text="second",
    ).build_incremental(cache)

    assert first.skill_context_message is not None
    assert second.skill_context_message is not None
    assert second.skill_context_message is not first.skill_context_message
    assert "Body-Hash: new" in second.skill_context_message.content
    assert "Old docs rules" not in second.skill_context_message.content


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
        current_user_text="first",
    ).build_incremental(cache)
    second, cache = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
        session_date="2026-06-06 CST",
        summary="new summary",
        current_user_text="second",
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
        current_user_text="current request",
    ).build()
    second = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
        session_date="2026-06-06 CST",
        current_user_text="current request",
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
        skill_context_content=render_skill_context(["Skill instructions from: docs\nSkill: docs"]),
        current_user_text="describe image",
    ).build()

    context.apply_to_messages(messages)

    assert isinstance(messages[0], SystemMessage)
    assert isinstance(messages[1], HumanMessage)
    assert messages[1].content.startswith(SKILL_CONTEXT_MARKER)
    assert all(not isinstance(message, SystemMessage) for message in messages[1:])
    assert isinstance(messages[-1], HumanMessage)
    assert isinstance(messages[-1].content, list)
    assert messages[-1].content[0]["type"] == "text"
    assert "Runtime State" in messages[-1].content[0]["text"]
    assert "Current DateTime" not in messages[-1].content[0]["text"]
    assert "Active Skills" not in messages[-1].content[0]["text"]
    assert messages[-1].content[1]["type"] == "text"
    assert messages[-1].content[2]["type"] == "image_url"


def test_runtime_context_drops_previous_skill_context_overlay(tmp_path):
    messages = [
        HumanMessage(content=f"{SKILL_CONTEXT_MARKER}\n\n## Skill: docs\nBody-Hash: old\n\nOld body"),
        HumanMessage(content="current request"),
    ]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
        skill_context_content=f"{SKILL_CONTEXT_MARKER}\n\n## Skill: docs\nBody-Hash: new\n\nNew body",
        current_user_text="current request",
    ).build()

    context.apply_to_messages(messages)

    assert len([m for m in messages if isinstance(m, HumanMessage) and str(m.content).startswith(SKILL_CONTEXT_MARKER)]) == 1
    assert "Old body" not in "\n".join(str(message.content) for message in messages)
    assert "New body" in messages[1].content


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
        current_user_text="current request",
    ).build()

    context.apply_to_messages(messages)

    historical_tool = next(message for message in messages if isinstance(message, ToolMessage))
    assert SKILL_TOOL_CONTEXT_STRIPPED_MARKER in historical_tool.content
    assert "tdd sha256=abc123 source=bundled" in historical_tool.content
    assert "Full skill body" not in historical_tool.content


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
        current_user_text="current request",
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
        current_user_text="current request",
    ).build()

    context.apply_to_messages(messages)

    historical_tool = next(message for message in messages if isinstance(message, ToolMessage))
    assert historical_tool.content == stripped_output


def test_skill_context_reference_library_marks_inactive_skills_not_active(tmp_path):
    messages = [HumanMessage(content="current request")]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
        skill_context_content=render_skill_context([
            "## Skill: brainstorm\nBody-Hash: aaa\n\nMUST brainstorm before implementation",
            "## Skill: tdd\nBody-Hash: bbb\n\nTDD rules",
        ]),
        active_workflow_summaries=["tdd (implement persona)"],
        current_user_text="current request",
    ).build()

    context.apply_to_messages(messages)

    skill_context = messages[1].content
    task_context = messages[-1].content
    assert "reference library" in skill_context
    assert "Treat inactive skill bodies as reference material only." in skill_context
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
        skill_context_content=render_skill_context([
            "## Skill: tdd\nBody-Hash: bbb\n\nFull TDD body",
        ]),
        active_workflow_summaries=["tdd (implement persona)"],
        current_user_text="current request",
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
        current_user_text="current request",
    ).build()

    context.apply_to_messages(messages)

    current_tool = next(message for message in messages if isinstance(message, ToolMessage))
    assert SKILL_TOOL_CONTEXT_MARKER in current_tool.content
    assert "Full skill body" in current_tool.content
    assert SKILL_TOOL_CONTEXT_STRIPPED_MARKER not in current_tool.content


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
    assert infer_task_intent("看看 voidx 的 agent 编排") == TaskIntent.CODING
    assert infer_task_intent("可以看看这个项目") == TaskIntent.CODING
    assert infer_task_intent("有什么更好的优化方案") == TaskIntent.CODING
    assert infer_task_intent("可以，直接改") == TaskIntent.CODING
    assert infer_task_intent("对，可以") == TaskIntent.GENERAL


def test_keyword_intent_defaults_broad_problem_to_coding():
    assert infer_task_intent("有什么问题吗") == TaskIntent.CODING
    assert infer_task_intent("看看这个问题") == TaskIntent.CODING
    assert infer_task_intent("这个问题怎么解决") == TaskIntent.CODING
    assert infer_task_intent("这个报错问题怎么处理") == TaskIntent.CODING


def test_keyword_intent_uses_word_boundaries_for_short_english_hints():
    assert infer_task_intent("fix this issue") == TaskIntent.CODING
    assert infer_task_intent("prefix handling") == TaskIntent.CODING
    assert infer_task_intent("suffix handling") == TaskIntent.CODING
    assert infer_task_intent("这个可以改吗") == TaskIntent.CODING
    assert infer_task_intent("这个可以开始了吗") == TaskIntent.CODING
    assert infer_task_intent("谢谢") == TaskIntent.GENERAL


def test_current_task_state_records_intent_and_implementation_gate(tmp_path):
    messages = [HumanMessage(content="看看这个项目")]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
        current_user_text="看看这个项目",
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
        current_user_text="实现这个功能",
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
        current_user_text="实现这个功能",
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

    assert (
        "Workflow run state: tdd=active "
        "goal_type=feature source=workflow reason=implement persona"
    ) in messages[-1].content
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
        current_user_text="review 完并修复问题",
        task_state=TaskState(
            current_intent=TaskIntent.CODING,
            workflow_route=WorkflowRoute(start="review", end="verify"),
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
        current_user_text="处理 review 反馈",
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
        current_user_text="继续",
        task_state=TaskState(current_intent=TaskIntent.CODING),
    ).build()

    context.apply_to_messages(messages)

    assert "User language: Chinese (Simplified) [zh-CN]" in messages[-1].content
    assert "User language preference: Chinese (Simplified) [zh-CN]" in messages[-1].content
    assert "Language instruction: Prefer responding in Chinese (Simplified)" in messages[-1].content
    assert "User tone: direct" in messages[-1].content
    assert "Tone instruction: Be direct and practical. Lead with the answer or action." in messages[-1].content


def test_current_task_state_records_refined_intent_without_visible_tools(tmp_path):
    messages = [HumanMessage(content="继续")]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
        current_user_text="继续",
        task_state=TaskState(current_intent=TaskIntent.CODING),
    ).build()

    context.apply_to_messages(messages)

    assert "Current persona: voidx" in messages[-1].content
    assert "Runtime-visible tools" not in messages[-1].content


def test_current_task_state_records_multiturn_approval_state(tmp_path):
    messages = [HumanMessage(content="给个方案")]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
        current_user_text="给个方案",
        task_state=TaskState(
            current_intent=TaskIntent.CODING,
            pending_approval=PendingApproval(scope="优化 runtime context"),
            current_goal=goal_from_text("优化 runtime context", goal_type=GoalType.DESIGN),
        ),
    ).build()

    context.apply_to_messages(messages)

    assert "Intent: coding" in messages[-1].content
    assert "Goal type: design" in messages[-1].content
    assert "Pending approval: implementation scope=优化 runtime context" in messages[-1].content
    assert "Suggestion: use plan_checkpoint" in messages[-1].content


def test_current_task_state_records_goal_run(tmp_path):
    messages = [HumanMessage(content="给个方案")]
    context = RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.GOAL,
        current_user_text="给个方案",
        task_state=TaskState(
            current_intent=TaskIntent.CODING,
            current_goal=goal_from_text("优化 markdown 渲染截断", goal_type=GoalType.DESIGN),
        ),
    ).build()

    context.apply_to_messages(messages)

    assert "Current persona: voidx" in messages[-1].content
    assert "Goal type: design" in messages[-1].content
    assert "Goal target: 优化 markdown 渲染截断" in messages[-1].content
