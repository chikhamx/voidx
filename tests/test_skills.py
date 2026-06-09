import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

from voidx.llm.instruction import InstructionService
from voidx.skills.context import SKILL_CONTEXT_MARKER, SKILL_CONTEXT_SCOPE
from voidx.skills.registry import SkillRegistry, parse_skill_file
from voidx.skills.runtime import (
    SkillActivationSource,
    SkillRunState,
    SkillRunStatus,
    SkillStateEvent,
    SkillStateEventKind,
    advance_skill_states,
)
from voidx.skills.schema import SkillSelectionConfig
from voidx.skills.service import SkillService


def _write_skill(root: Path, dirname: str, text: str) -> Path:
    skill_dir = root / dirname
    skill_dir.mkdir(parents=True)
    path = skill_dir / "SKILL.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_parse_skill_file_reads_frontmatter_and_body(tmp_path):
    path = _write_skill(
        tmp_path,
        "docs",
        """---
name: docs-helper
description: Helps write docs
enabled: false
triggers:
  - documentation
  - README
---
# Instructions
Write clear docs.
""",
    )

    skill = parse_skill_file(path, scope="project")

    assert skill.name == "docs-helper"
    assert skill.meta.description == "Helps write docs"
    assert skill.meta.enabled is False
    assert skill.meta.triggers == ["documentation", "README"]
    assert skill.body == "# Instructions\nWrite clear docs."
    assert skill.meta.scope == "project"


def test_parse_skill_file_no_frontmatter(tmp_path):
    path = _write_skill(tmp_path, "bare", "Just a body with no frontmatter.")
    skill = parse_skill_file(path, scope="project")
    assert skill.name == "bare"
    assert skill.meta.description == ""
    assert skill.meta.enabled is True
    assert skill.meta.triggers == []
    assert skill.body == "Just a body with no frontmatter."


def test_parse_skill_file_unclosed_frontmatter(tmp_path):
    path = _write_skill(tmp_path, "bad", "---\nname: bad\nno closing")
    from voidx.skills.registry import SkillParseError
    import pytest
    with pytest.raises(SkillParseError, match="Unclosed frontmatter"):
        parse_skill_file(path, scope="project")


def test_parse_skill_file_quoted_values(tmp_path):
    path = _write_skill(
        tmp_path,
        "quoted",
        "---\nname: quoted\ndescription: 'has: colons, and stuff'\n---\nbody",
    )
    skill = parse_skill_file(path, scope="project")
    assert skill.meta.description == "has: colons, and stuff"


def test_parse_skill_file_inline_list(tmp_path):
    path = _write_skill(
        tmp_path,
        "inline",
        "---\nname: inline\ntriggers: [alpha, beta]\n---\nbody",
    )
    skill = parse_skill_file(path, scope="project")
    assert skill.meta.triggers == ["alpha", "beta"]


def test_parse_skill_file_multiline_description(tmp_path):
    path = _write_skill(
        tmp_path,
        "multi",
        '---\nname: multi\ndescription: >\n  This is a long\n  description that spans\n  multiple lines\n---\nbody',
    )
    skill = parse_skill_file(path, scope="project")
    assert "long" in skill.meta.description
    assert "multiple" in skill.meta.description


def test_parse_skill_file_comments_ignored(tmp_path):
    path = _write_skill(
        tmp_path,
        "commented",
        "---\n# this is a comment\nname: commented\n---\nbody",
    )
    skill = parse_skill_file(path, scope="project")
    assert skill.name == "commented"


def test_parse_skill_file_empty_name_falls_back_to_dir(tmp_path):
    path = _write_skill(
        tmp_path,
        "my-skill",
        "---\nname: ''\n---\nbody",
    )
    skill = parse_skill_file(path, scope="project")
    assert skill.name == "my-skill"


def test_registry_discovers_global_and_project_with_project_override(tmp_path):
    global_dir = tmp_path / "global"
    project_dir = tmp_path / "workspace" / ".voidx" / "skills"
    _write_skill(global_dir, "python", "---\nname: python\n---\nglobal body")
    _write_skill(global_dir, "docs", "---\nname: docs\n---\ndocs body")
    _write_skill(project_dir, "python", "---\nname: python\n---\nproject body")

    registry = SkillRegistry(
        str(tmp_path / "workspace"),
        bundled_dir=tmp_path / "bundled",
        global_dir=global_dir,
        project_dir=project_dir,
    )

    skills = registry.discover()

    assert [skill.name for skill in skills] == ["docs", "python"]
    assert registry.get("python").body == "project body"
    assert registry.get("python").meta.scope == "project"
    assert registry.get("docs").meta.scope == "global"


def test_registry_discovers_bundled_superpower_skills_by_default(tmp_path):
    registry = SkillRegistry(
        str(tmp_path / "workspace"),
        global_dir=tmp_path / "global",
        project_dir=tmp_path / "workspace" / ".voidx" / "skills",
    )

    skills = {skill.name: skill for skill in registry.discover()}

    for name in {
        "systematic-debugging",
        "test-driven-development",
        "verification-before-completion",
        "receiving-code-review",
        "requesting-code-review",
        "writing-plans",
    }:
        assert name in skills
        assert skills[name].meta.scope == "bundled"
        assert "voidx" in skills[name].body


def test_registry_project_and_global_override_bundled_skills(tmp_path):
    global_dir = tmp_path / "global"
    project_dir = tmp_path / "workspace" / ".voidx" / "skills"
    _write_skill(global_dir, "systematic-debugging", "---\nname: systematic-debugging\n---\nglobal body")
    _write_skill(project_dir, "systematic-debugging", "---\nname: systematic-debugging\n---\nproject body")

    registry = SkillRegistry(
        str(tmp_path / "workspace"),
        global_dir=global_dir,
        project_dir=project_dir,
    )

    skill = registry.get("systematic-debugging")

    assert skill is not None
    assert skill.body == "project body"
    assert skill.meta.scope == "project"


def test_skill_service_returns_enabled_bundled_skills_after_overrides(tmp_path):
    global_dir = tmp_path / "global"
    project_dir = tmp_path / "workspace" / ".voidx" / "skills"
    _write_skill(project_dir, "systematic-debugging", "---\nname: systematic-debugging\n---\nProject body")

    service = SkillService(
        SkillRegistry(
            str(tmp_path / "workspace"),
            global_dir=global_dir,
            project_dir=project_dir,
        ),
        selection=SkillSelectionConfig(disabled={"verification-before-completion"}),
    )

    bundled_names = {skill.name for skill in service.enabled_bundled_skills()}

    assert "systematic-debugging" not in bundled_names
    assert "verification-before-completion" not in bundled_names
    assert "test-driven-development" in bundled_names


def test_skill_service_selects_explicit_and_trigger_matches(tmp_path):
    project_dir = tmp_path / "workspace" / ".voidx" / "skills"
    _write_skill(
        project_dir,
        "python",
        "---\nname: python\ntriggers: [pytest, pydantic]\n---\nPython rules",
    )
    _write_skill(project_dir, "docs", "---\nname: docs\n---\nDocs rules")
    service = SkillService(
        SkillRegistry(
            str(tmp_path / "workspace"),
            bundled_dir=tmp_path / "bundled",
            global_dir=tmp_path / "global",
            project_dir=project_dir,
        )
    )

    explicit = service.select("use $docs for this")
    trigger = service.select("please add pytest coverage")

    assert [match.name for match in explicit] == ["docs"]
    assert explicit[0].reason == "explicit"
    assert [match.name for match in trigger] == ["python"]
    assert trigger[0].reason == "trigger:pytest"


def test_skill_body_parse_cache_keeps_activation_dynamic(tmp_path, monkeypatch):
    project_dir = tmp_path / "workspace" / ".voidx" / "skills"
    _write_skill(project_dir, "alpha", "---\nname: alpha\ntriggers: [alpha]\n---\nAlpha rules")
    _write_skill(project_dir, "beta", "---\nname: beta\ntriggers: [beta]\n---\nBeta rules")

    original_read_text = Path.read_text
    calls: list[Path] = []

    def counting_read_text(self, *args, **kwargs):
        calls.append(self)
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read_text)

    def service() -> SkillService:
        return SkillService(
            SkillRegistry(
                str(tmp_path / "workspace"),
                bundled_dir=tmp_path / "bundled",
                global_dir=tmp_path / "global",
                project_dir=project_dir,
            )
        )

    first = service().select("use alpha")
    first_call_count = len(calls)
    second = service().select("use beta")

    assert [match.name for match in first] == ["alpha"]
    assert [match.name for match in second] == ["beta"]
    assert first_call_count == 2
    assert len(calls) == first_call_count


def test_skill_service_selects_bundled_superpower_triggers(tmp_path):
    service = SkillService(
        SkillRegistry(
            str(tmp_path / "workspace"),
            global_dir=tmp_path / "global",
            project_dir=tmp_path / "workspace" / ".voidx" / "skills",
        )
    )

    debug = service.select("pytest failed with a traceback")
    tdd = service.select("implement this feature")
    feedback = service.select("review feedback says this endpoint is overbuilt")

    assert [match.name for match in debug] == ["systematic-debugging"]
    assert [match.name for match in tdd] == ["test-driven-development"]
    assert [match.name for match in feedback] == ["receiving-code-review"]


def test_skill_service_selects_workflow_policy_by_role_and_intent(tmp_path):
    service = SkillService(
        SkillRegistry(
            str(tmp_path / "workspace"),
            global_dir=tmp_path / "global",
            project_dir=tmp_path / "workspace" / ".voidx" / "skills",
        )
    )

    implement = service.select(
        "对，可以",
        agent="implement",
        task_intent="implement",
    )
    debug = service.select(
        "fix this bug",
        agent="orchestrator",
        task_intent="debug",
    )
    plan = service.select(
        "给个方案",
        agent="plan",
        task_intent="design",
        interaction_mode="plan",
    )

    assert [match.name for match in implement] == [
        "test-driven-development",
        "verification-before-completion",
    ]
    assert [match.name for match in debug][:2] == [
        "systematic-debugging",
        "verification-before-completion",
    ]
    assert "implement role" in implement[0].reason
    assert "debug intent" in debug[0].reason
    assert [match.name for match in plan] == ["brainstorming", "writing-plans"]
    assert plan[0].reason == "design/create intent"
    assert plan[1].reason == "plan role"


def test_skill_service_activates_requesting_code_review_for_review_intent(tmp_path):
    service = SkillService(
        SkillRegistry(
            str(tmp_path / "workspace"),
            global_dir=tmp_path / "global",
            project_dir=tmp_path / "workspace" / ".voidx" / "skills",
        )
    )

    matches = service.select(
        "review 一下代码",
        agent="orchestrator",
        task_intent="review",
        scopes=("bundled",),
    )

    assert [match.name for match in matches] == ["requesting-code-review"]
    assert matches[0].reason == "review intent"


def test_skill_service_activates_receiving_code_review_for_feedback(tmp_path):
    service = SkillService(
        SkillRegistry(
            str(tmp_path / "workspace"),
            global_dir=tmp_path / "global",
            project_dir=tmp_path / "workspace" / ".voidx" / "skills",
        )
    )

    matches = service.select(
        "review feedback says this path is unsafe",
        agent="orchestrator",
        task_intent="review",
        scopes=("bundled",),
    )

    assert [match.name for match in matches] == ["receiving-code-review"]
    assert matches[0].reason == "review feedback"


def test_skill_transitions_are_soft_constraints_documented():
    doc = Path(__file__).parent.parent / "docs" / "archive" / "skill-state-machine-2026-06-08.md"
    text = doc.read_text(encoding="utf-8")

    assert "transition 是 **soft constraint**" in text
    assert "runtime 不强制推进依赖链" in text
    assert "transition_to: list[str]" in text


def test_bundled_workflow_selection_excludes_user_scoped_skills(tmp_path):
    project_dir = tmp_path / "workspace" / ".voidx" / "skills"
    _write_skill(
        project_dir,
        "test-driven-development",
        "---\nname: test-driven-development\n---\nProject override body",
    )
    service = SkillService(
        SkillRegistry(
            str(tmp_path / "workspace"),
            global_dir=tmp_path / "global",
            project_dir=project_dir,
        )
    )

    matches = service.select(
        "implement this feature",
        agent="implement",
        task_intent="implement",
        scopes=("bundled",),
    )

    assert [match.name for match in matches] == ["verification-before-completion"]


def test_skill_service_excludes_active_names_from_selection(tmp_path):
    service = SkillService(
        SkillRegistry(
            str(tmp_path / "workspace"),
            global_dir=tmp_path / "global",
            project_dir=tmp_path / "workspace" / ".voidx" / "skills",
        )
    )

    matches = service.select(
        "implement this feature",
        agent="implement",
        task_intent="implement",
        scopes=("bundled",),
        exclude_names=("test-driven-development",),
    )

    assert [match.name for match in matches] == ["verification-before-completion"]


def test_skill_service_returns_structured_skill_runs(tmp_path):
    service = SkillService(
        SkillRegistry(
            str(tmp_path / "workspace"),
            global_dir=tmp_path / "global",
            project_dir=tmp_path / "workspace" / ".voidx" / "skills",
        )
    )

    runs = service.select_runs(
        "对，可以",
        agent="implement",
        task_intent="implement",
        phase="implement",
        scope="优化 runtime context",
        turn_count=3,
    )

    assert [run.name for run in runs] == [
        "test-driven-development",
        "verification-before-completion",
    ]
    assert {run.status for run in runs} == {SkillRunStatus.ACTIVE}
    assert {run.source for run in runs} == {SkillActivationSource.WORKFLOW}
    assert runs[0].phase == "implement"
    assert runs[0].scope == "优化 runtime context"
    assert runs[0].activated_turn == 3
    assert runs[0].body_hash
    assert runs[0].transition_to == ["verification-before-completion"]
    assert runs[1].transition_to == ["requesting-code-review"]


def test_skill_run_state_from_match_includes_transition_targets(tmp_path):
    service = SkillService(
        SkillRegistry(
            str(tmp_path / "workspace"),
            global_dir=tmp_path / "global",
            project_dir=tmp_path / "workspace" / ".voidx" / "skills",
        )
    )

    match = service.select(
        "implement this feature",
        agent="implement",
        task_intent="implement",
        scopes=("bundled",),
    )[0]

    run = SkillRunState.from_match(match)

    assert run.name == "test-driven-development"
    assert run.transition_to == ["verification-before-completion"]


def test_skill_state_summary_includes_transition_hint():
    run = SkillRunState(
        name="test-driven-development",
        status=SkillRunStatus.ACTIVE,
        source=SkillActivationSource.WORKFLOW,
        reason="implement intent",
        transition_to=["verification-before-completion"],
    )

    assert "next=verification-before-completion" in run.state_summary()


def test_advance_skill_states_marks_satisfied_from_evidence():
    run = SkillRunState(
        name="test-driven-development",
        status=SkillRunStatus.ACTIVE,
        transition_to=[],
    )

    states = advance_skill_states(
        [run],
        [
            SkillStateEvent(
                skill="test-driven-development",
                kind=SkillStateEventKind.SATISFIED,
                ref="tool:pytest",
                ok=True,
                summary="focused tests passed",
            )
        ],
        turn_count=4,
    )

    tdd = next(item for item in states if item.name == "test-driven-development")
    assert tdd.status == SkillRunStatus.SATISFIED
    assert tdd.updated_turn == 4
    assert tdd.evidence[0].summary == "focused tests passed"


def test_advance_skill_states_does_not_mark_pending_satisfied():
    states = advance_skill_states(
        [SkillRunState(name="test-driven-development", status=SkillRunStatus.PENDING)],
        [{"skill": "test-driven-development", "kind": "satisfied"}],
        turn_count=4,
    )

    assert states[0].status == SkillRunStatus.PENDING
    assert "verification-before-completion" not in [run.name for run in states]


def test_advance_skill_states_initializes_missing_run_from_event_kind():
    blocked = advance_skill_states(
        [],
        [{"skill": "systematic-debugging", "kind": "blocked", "reason": "needs repro"}],
    )
    skipped = advance_skill_states(
        [],
        [{"skill": "requesting-code-review", "kind": "skipped"}],
    )
    satisfied = advance_skill_states(
        [],
        [{"skill": "test-driven-development", "kind": "satisfied"}],
    )

    assert blocked[0].status == SkillRunStatus.BLOCKED
    assert blocked[0].blocked_reason == "needs repro"
    assert skipped[0].status == SkillRunStatus.SKIPPED
    assert satisfied[0].status == SkillRunStatus.PENDING


def test_advance_skill_states_activates_transition_target():
    states = advance_skill_states(
        [
            SkillRunState(
                name="test-driven-development",
                status=SkillRunStatus.ACTIVE,
                phase="implement",
                scope="runtime",
                transition_to=["verification-before-completion"],
            )
        ],
        [
            {
                "skill": "test-driven-development",
                "kind": "satisfied",
                "summary": "implementation complete",
            }
        ],
        turn_count=5,
    )

    by_name = {run.name: run for run in states}
    assert by_name["test-driven-development"].status == SkillRunStatus.SATISFIED
    successor = by_name["verification-before-completion"]
    assert successor.status == SkillRunStatus.ACTIVE
    assert successor.source == SkillActivationSource.TRANSITION
    assert successor.reason == "transition from test-driven-development"
    assert successor.phase == "implement"
    assert successor.scope == "runtime"


def test_advance_skill_states_does_not_advance_without_evidence():
    run = SkillRunState(
        name="test-driven-development",
        status=SkillRunStatus.ACTIVE,
        transition_to=["verification-before-completion"],
    )

    states = advance_skill_states([run], [], turn_count=6)

    assert [item.name for item in states] == ["test-driven-development"]
    assert states[0].status == SkillRunStatus.ACTIVE


def test_advance_skill_states_does_not_duplicate_existing_successor():
    states = advance_skill_states(
        [
            SkillRunState(
                name="test-driven-development",
                status=SkillRunStatus.ACTIVE,
                transition_to=["verification-before-completion"],
            ),
            SkillRunState(
                name="verification-before-completion",
                status=SkillRunStatus.ACTIVE,
                reason="implement lifecycle",
            ),
        ],
        [{"skill": "test-driven-development", "kind": "satisfied"}],
    )

    assert [run.name for run in states].count("verification-before-completion") == 1
    verification = next(run for run in states if run.name == "verification-before-completion")
    assert verification.reason == "implement lifecycle"


def test_blocked_or_skipped_skill_does_not_trigger_successor():
    blocked = advance_skill_states(
        [
            SkillRunState(
                name="test-driven-development",
                status=SkillRunStatus.BLOCKED,
                transition_to=["verification-before-completion"],
            )
        ],
        [{"skill": "test-driven-development", "kind": "satisfied"}],
    )
    skipped = advance_skill_states(
        [
            SkillRunState(
                name="test-driven-development",
                status=SkillRunStatus.ACTIVE,
                transition_to=["verification-before-completion"],
            )
        ],
        [{"skill": "test-driven-development", "kind": "skipped"}],
    )

    assert [run.name for run in blocked] == ["test-driven-development"]
    assert blocked[0].status == SkillRunStatus.BLOCKED
    assert [run.name for run in skipped] == ["test-driven-development"]
    assert skipped[0].status == SkillRunStatus.SKIPPED


def test_blocked_skill_can_reactivate_when_condition_clears():
    states = advance_skill_states(
        [
            SkillRunState(
                name="systematic-debugging",
                status=SkillRunStatus.BLOCKED,
                blocked_reason="needs repro",
            )
        ],
        [
            SkillStateEvent(
                skill="systematic-debugging",
                kind=SkillStateEventKind.UNBLOCKED,
                summary="repro added",
            )
        ],
        turn_count=7,
    )

    assert states[0].status == SkillRunStatus.ACTIVE
    assert states[0].blocked_reason == ""
    assert states[0].updated_turn == 7


def test_skill_service_returns_activation_summaries(tmp_path):
    service = SkillService(
        SkillRegistry(
            str(tmp_path / "workspace"),
            global_dir=tmp_path / "global",
            project_dir=tmp_path / "workspace" / ".voidx" / "skills",
        )
    )

    summaries = service.activation_summaries(
        "对，可以",
        agent="implement",
        task_intent="implement",
    )

    assert summaries == [
        "test-driven-development (implement role)",
        "verification-before-completion (implement lifecycle)",
    ]


def test_skill_service_available_summaries_exclude_bundled_body(tmp_path):
    project_dir = tmp_path / "workspace" / ".voidx" / "skills"
    _write_skill(
        project_dir,
        "docs",
        "---\nname: docs\ndescription: Write docs\n---\nDocs body",
    )
    service = SkillService(
        SkillRegistry(
            str(tmp_path / "workspace"),
            global_dir=tmp_path / "global",
            project_dir=project_dir,
        )
    )

    summaries = service.available_skill_summaries()

    assert summaries == ["- docs: Write docs"]
    assert "Docs body" not in "\n".join(summaries)


@pytest.mark.asyncio
async def test_instruction_service_system_includes_available_skills_section(tmp_path):
    project_dir = tmp_path / ".voidx" / "skills"
    _write_skill(
        project_dir,
        "docs",
        "---\nname: docs\ndescription: Write docs\n---\nDocs body",
    )

    instructions = await InstructionService(str(tmp_path)).system()

    joined = "\n\n".join(instructions)
    assert "## Available Skills" in joined
    assert "- docs: Write docs" in joined
    assert "Docs body" not in joined
    assert "systematic-debugging" not in joined


@pytest.mark.asyncio
async def test_skill_context_message_contains_all_bundled_skills(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

    context = await InstructionService(str(tmp_path)).skill_context_for(
        "hello",
        task_intent="chat",
    )
    expected = SkillService(
        SkillRegistry(
            str(tmp_path),
            global_dir=tmp_path / "home" / ".voidx" / "skills",
            project_dir=tmp_path / ".voidx" / "skills",
        )
    ).enabled_bundled_skills()

    assert context.content.startswith(SKILL_CONTEXT_MARKER)
    assert f"Scope: {SKILL_CONTEXT_SCOPE}" in context.content
    assert "reference library" in context.content
    assert "Do not treat inactive skill bodies as active instructions." in context.content
    assert expected
    for skill in expected:
        assert f"## Skill: {skill.name}" in context.content


@pytest.mark.asyncio
async def test_skill_context_message_stable_across_intent_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

    instruction = InstructionService(str(tmp_path))
    inspect_context = await instruction.skill_context_for(
        "看看代码",
        agent="orchestrator",
        task_intent="inspect",
    )
    implement_context = await instruction.skill_context_for(
        "Implement the feature",
        agent="implement",
        task_intent="implement",
    )

    assert inspect_context.content == implement_context.content
    assert inspect_context.active != implement_context.active
    assert any("test-driven-development" in item for item in implement_context.active)
    assert any("verification-before-completion" in item for item in implement_context.active)


def test_skill_service_respects_disabled_before_enabled(tmp_path):
    project_dir = tmp_path / "workspace" / ".voidx" / "skills"
    _write_skill(project_dir, "docs", "---\nname: docs\nenabled: false\n---\nDocs rules")
    _write_skill(project_dir, "python", "---\nname: python\n---\nPython rules")

    service = SkillService(
        SkillRegistry(
            str(tmp_path / "workspace"),
            bundled_dir=tmp_path / "bundled",
            global_dir=tmp_path / "global",
            project_dir=project_dir,
        ),
        selection=SkillSelectionConfig(enabled={"docs"}, disabled={"python"}),
    )

    assert [skill.name for skill in service.enabled_skills()] == ["docs"]
    assert service.select("$docs")[0].name == "docs"
    assert service.select("$python") == []


def test_registry_discover_caches_results(tmp_path):
    project_dir = tmp_path / "workspace" / ".voidx" / "skills"
    _write_skill(project_dir, "alpha", "---\nname: alpha\n---\nalpha body")

    registry = SkillRegistry(
        str(tmp_path / "workspace"),
        bundled_dir=tmp_path / "bundled",
        global_dir=tmp_path / "global",
        project_dir=project_dir,
    )

    first = registry.discover()
    second = registry.discover()
    assert first is second


def test_registry_discover_refreshes_when_skill_file_changes(tmp_path):
    project_dir = tmp_path / "workspace" / ".voidx" / "skills"
    skill_file = _write_skill(project_dir, "alpha", "---\nname: alpha\n---\nalpha body")

    registry = SkillRegistry(
        str(tmp_path / "workspace"),
        bundled_dir=tmp_path / "bundled",
        global_dir=tmp_path / "global",
        project_dir=project_dir,
    )

    first = registry.discover()
    skill_file.write_text("---\nname: alpha\n---\nalpha body changed", encoding="utf-8")
    second = registry.discover()

    assert first is not second
    assert second[0].body == "alpha body changed"


def test_registry_invalidate_clears_cache(tmp_path):
    project_dir = tmp_path / "workspace" / ".voidx" / "skills"
    _write_skill(project_dir, "alpha", "---\nname: alpha\n---\nalpha body")

    registry = SkillRegistry(
        str(tmp_path / "workspace"),
        bundled_dir=tmp_path / "bundled",
        global_dir=tmp_path / "global",
        project_dir=project_dir,
    )

    first = registry.discover()
    registry.invalidate()
    second = registry.discover()
    assert first is not second
    assert [s.name for s in first] == [s.name for s in second]


def test_skill_service_trigger_match_uses_word_boundary(tmp_path):
    project_dir = tmp_path / "workspace" / ".voidx" / "skills"
    _write_skill(
        project_dir,
        "test-skill",
        "---\nname: test-skill\ntriggers: [test]\n---\nTest rules",
    )
    service = SkillService(
        SkillRegistry(
            str(tmp_path / "workspace"),
            bundled_dir=tmp_path / "bundled",
            global_dir=tmp_path / "global",
            project_dir=project_dir,
        )
    )

    assert service.select("run the latest version") == []
    assert service.select("win the contest") == []
    assert service.select("write a test for this") != []


def test_skill_service_renders_instruction_with_source_path(tmp_path):
    project_dir = tmp_path / "workspace" / ".voidx" / "skills"
    path = _write_skill(
        project_dir,
        "docs",
        "---\nname: docs\ndescription: Write documentation\n---\nDocs rules",
    )
    service = SkillService(
        SkillRegistry(
            str(tmp_path / "workspace"),
            bundled_dir=tmp_path / "bundled",
            global_dir=tmp_path / "global",
            project_dir=project_dir,
        )
    )

    rendered = service.instructions_for("$docs")[0]

    assert f"Path: {path.resolve()}" in rendered
    assert "## Skill: docs" in rendered
    assert "Source: project" in rendered
    assert "Body-Hash:" in rendered
    assert "Description: Write documentation" in rendered
    assert "Docs rules" in rendered
