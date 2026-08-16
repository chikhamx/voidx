import sys
from pathlib import Path

import pytest

from voidx.agent.application.prompts import (
    BASE_SYSTEM,
    PERSONA_MODEL,
    WORKFLOW_RUNTIME,
    BaseSystemPrompt,
    PromptRule,
    build_base_system,
    persona_prompt,
)


def test_prompt_rule_renders_labelled_and_plain_rules():
    assert PromptRule(label="Natural.", detail="Speak plainly.").render() == "**Natural.** Speak plainly."
    assert PromptRule(label="", detail="Use tools for facts.").render() == "Use tools for facts."


def test_prompt_rule_name_does_not_render():
    assert PromptRule(name="tone", label="Natural.", detail="Speak plainly.").render() == "**Natural.** Speak plainly."


def test_base_system_prompt_has_canonical_rules():
    assert BASE_SYSTEM.identity == "You are voidx, an autonomous coding agent."

    rendered = BASE_SYSTEM.render()
    assert rendered.startswith("You are voidx, an autonomous coding agent.")
    assert "## Communication Style" in rendered
    assert "## Global Rules" in rendered
    assert "### Runtime Rules" in rendered
    assert "### Workspace Rules" in rendered
    assert "### Verification Rules" in rendered
    assert "### Collaboration Rules" in rendered
    assert "### Delegation Rules" not in rendered
    assert "## Workflow Runtime" not in rendered
    assert "Do not discuss personas, workflow nodes, agents, or runtime mechanics in user-facing replies" not in rendered
    assert "Preserve user work in a dirty tree" in rendered
    assert "Subagents do not interact with the user" not in rendered
    assert "Follow user requests unless they conflict with higher-priority instructions or safety constraints." in rendered
    assert "Ask only the minimum questions needed to proceed, preferably one at a time." in rendered
    assert "turn operation='start'" not in rendered
    assert "turn operation='stop'" not in rendered
    assert "Use active workflow gates as completion and transition criteria." in rendered
    assert [rule.name for rule in BASE_SYSTEM.communication_style[:2]] == ["language", "tone"]
    assert {rule.name for rule in BASE_SYSTEM.communication_style} == {
        "tone",
        "language",
        "concise",
        "progress_preamble",
        "summarize_results",
        "uncertainty",
        "todo_progress",
    }
    assert [rule.name for rule in BASE_SYSTEM.global_rules] == [
        "workflow_gates",
        "workspace_facts",
        "read_before_edit",
        "smallest_change",
        "preserve_dirty",
        "fresh_verification",
        "min_questions",
        "follow_requests",
    ]


def test_build_base_system_returns_default_for_auto_or_blank_language():
    assert build_base_system("") is BASE_SYSTEM
    assert build_base_system("auto") is BASE_SYSTEM
    assert build_base_system(" default ") is BASE_SYSTEM


def test_build_base_system_replaces_language_rule_for_known_language():
    prompt = build_base_system("zh-CN")

    assert prompt is not BASE_SYSTEM
    rendered = prompt.render()
    assert "**使用中文回复。**" in rendered
    assert "Prefer responding in Chinese (Simplified) unless the user explicitly asks otherwise." in rendered
    assert "**Match the user's language.**" not in rendered
    assert "**Natural and warm.**" in rendered
    assert "### Runtime Rules" in rendered


def test_build_base_system_preserves_custom_language_codes():
    prompt = build_base_system("pt-BR")

    rendered = prompt.render()
    assert "**Respond in pt-BR.**" in rendered
    assert "Prefer responding in pt-BR unless the user explicitly asks otherwise." in rendered
    assert "**Match the user's language.**" not in rendered


def test_build_base_system_preserves_plain_global_rules_for_custom_base():
    prompt = BaseSystemPrompt(
        identity=BASE_SYSTEM.identity,
        communication_style=[
            PromptRule(name="language", label="Match.", detail="Reply in the user's language."),
            PromptRule(name="tone", label="Natural.", detail="Speak plainly."),
        ],
        global_rules=[PromptRule(detail="Preserve this custom rule.")],
    )

    rendered = build_base_system("zh-CN", base_system=prompt).render()

    assert "**使用中文回复。**" in rendered
    assert "Preserve this custom rule." in rendered


def test_build_base_system_raises_when_language_anchor_missing():
    prompt = BaseSystemPrompt(
        identity=BASE_SYSTEM.identity,
        communication_style=[PromptRule(name="tone", label="Natural.", detail="Speak plainly.")],
        global_rules=[],
    )

    with pytest.raises(ValueError, match='name="language"'):
        build_base_system("zh-CN", base_system=prompt)


def test_persona_model_renders_all_personas_without_coordination_rules():
    rendered = persona_prompt()

    assert rendered == PERSONA_MODEL.render()
    assert rendered.startswith("## Persona Model")
    for persona in ("coordinate", "explore", "plan", "implement", "review"):
        assert f"**{persona}**" in rendered
    assert "## Coordination" not in rendered
    assert "## Responsibilities" not in rendered
    assert "## Rules" not in rendered
    assert "Switch persona" not in rendered


def test_workflow_runtime_uses_full_workflow_context():
    rendered = WORKFLOW_RUNTIME.render()

    assert rendered.startswith("## Workflow Runtime")
    assert "Current Task State is the sole source of active workflow nodes." in rendered
    assert "Only active workflow nodes are normative." in rendered
    assert "unless the user explicitly references another node by name" not in rendered
    assert "voidx has a structured workflow runtime." not in rendered
    assert len(WORKFLOW_RUNTIME.rules) == 2
    assert "VOIDX_WORKFLOW_CONTEXT" in rendered
    assert "## Workflow Node: debug" in rendered
    assert "## Workflow Node: design" in rendered
