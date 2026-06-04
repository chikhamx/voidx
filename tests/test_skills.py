import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from voidx.skills.registry import SkillRegistry, parse_skill_file
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
    assert [match.name for match in plan] == ["writing-plans"]
    assert plan[0].reason == "plan role"


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

    assert f"Skill instructions from: {path.resolve()}" in rendered
    assert "Skill: docs" in rendered
    assert "Description: Write documentation" in rendered
    assert "Docs rules" in rendered
