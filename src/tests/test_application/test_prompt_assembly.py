import pytest

from voidx.agent.application.prompts import (
    CHAT_PROFILE_SPEC,
    CODING_PROFILE_SPEC,
    GLOBAL_RULE_SECTIONS,
    STYLE_RULES,
    BaseSystemProfile,
    assemble_base_system,
)


def test_coding_profile_spec_assembles_full_base_system_without_capability_filter():
    prompt = assemble_base_system(CODING_PROFILE_SPEC, available_tools=None)
    rendered = prompt.render()

    assert prompt.identity == "You are voidx, an autonomous coding agent."
    assert "### Runtime Rules" in rendered
    assert "### Workspace Rules" in rendered
    assert "### Verification Rules" in rendered
    assert "### Collaboration Rules" in rendered
    assert "### Delegation Rules" not in rendered
    assert [rule.name for rule in prompt.communication_style] == [
        "language",
        "tone",
        "concise",
        "internals",
        "progress_preamble",
        "summarize_results",
        "uncertainty",
        "todo_progress",
    ]
    assert [rule.name for rule in prompt.global_rules] == [
        "workflow_gates",
        "workspace_facts",
        "read_before_edit",
        "smallest_change",
        "preserve_dirty",
        "fresh_verification",
        "min_questions",
        "follow_requests",
    ]


def test_chat_profile_spec_excludes_coding_only_rules():
    prompt = assemble_base_system(
        CHAT_PROFILE_SPEC,
        available_tools={"websearch", "webfetch", "mcp"},
    )
    rendered = prompt.render()

    assert prompt.identity == "You are voidx, a conversational assistant."
    style_names = {rule.name for rule in prompt.communication_style}
    assert "todo_progress" not in style_names
    assert "internals_chat" in style_names
    assert "coding assistant" not in rendered
    assert "conversational assistant" in rendered
    assert "summarize_results" in style_names
    assert "### Runtime Rules" not in rendered
    assert "### Workspace Rules" not in rendered
    assert "### Delegation Rules" not in rendered
    assert "### Verification Rules" in rendered
    assert "### Collaboration Rules" in rendered


def test_assemble_base_system_skips_rules_missing_required_tools():
    spec = BaseSystemProfile(
        identity="test",
        style_names=["progress_preamble", "todo_progress"],
        global_section_names={
            "Workspace Rules": ["workspace_facts", "preserve_dirty"],
        },
    )

    prompt = assemble_base_system(spec, available_tools={"websearch"})

    assert [rule.name for rule in prompt.communication_style] == ["progress_preamble"]
    assert [rule.name for rule in prompt.global_rules] == ["preserve_dirty"]


def test_assemble_base_system_raises_for_unknown_rule_names():
    spec = BaseSystemProfile(
        identity="test",
        style_names=["missing_style"],
        global_section_names={},
    )

    with pytest.raises(KeyError):
        assemble_base_system(spec, available_tools=None)

    spec = BaseSystemProfile(
        identity="test",
        style_names=[],
        global_section_names={"Workspace Rules": ["missing_rule"]},
    )

    with pytest.raises(KeyError):
        assemble_base_system(spec, available_tools=None)


def test_rule_pools_have_required_rule_metadata():
    assert STYLE_RULES["todo_progress"].requires == {"todo"}
    assert STYLE_RULES["summarize_results"].requires == set()
    assert GLOBAL_RULE_SECTIONS["Workspace Rules"]["workspace_facts"].requires == {
        "read",
        "find",
        "search",
    }


def test_skipped_rules_due_to_tool_gating_do_not_log_warnings(caplog):
    import logging

    spec = BaseSystemProfile(
        identity="test",
        style_names=["progress_preamble", "todo_progress"],
        global_section_names={
            "Workspace Rules": ["workspace_facts", "preserve_dirty"],
        },
    )

    with caplog.at_level(logging.DEBUG, logger="voidx.agent.application.prompts"):
        assemble_base_system(spec, available_tools={"websearch"})

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == []
