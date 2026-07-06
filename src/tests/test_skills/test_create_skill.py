"""Tests for SkillRegistry.create_skill — TDD red phase."""

import sys
from pathlib import Path


import pytest

from voidx.skills.registry import SkillRegistry, SKILL_NAME_RE
from voidx.skills.schema import SkillDefinition


class TestCreateSkill:
    def _registry(self, tmp_path: Path) -> SkillRegistry:
        return SkillRegistry(
            workspace=str(tmp_path),
            bundled_dir=tmp_path / "bundled",
            global_dir=tmp_path / "global",
            project_dir=tmp_path / "project",
        )

    def test_create_project_skill_writes_skill_md(self, tmp_path):
        reg = self._registry(tmp_path)

        path = reg.create_skill("react-patterns", "React patterns", "Use hooks.")

        assert path is not None
        assert path == tmp_path / "project" / "react-patterns" / "SKILL.md"
        assert path.exists()
        text = path.read_text(encoding="utf-8")
        assert "name: react-patterns" in text
        assert "description: React patterns" in text
        assert "enabled: true" in text
        assert "Use hooks." in text

    def test_create_global_skill_writes_to_global_dir(self, tmp_path):
        reg = self._registry(tmp_path)

        path = reg.create_skill("global-helper", "Helper", "Body.", scope="global")

        assert path is not None
        assert path == tmp_path / "global" / "global-helper" / "SKILL.md"
        assert path.exists()

    def test_create_creates_parent_dirs(self, tmp_path):
        reg = self._registry(tmp_path)
        assert not (tmp_path / "project").exists()

        path = reg.create_skill("new-skill", "Desc", "Body.")

        assert path is not None
        assert path.parent.exists()

    def test_create_returns_none_if_already_exists(self, tmp_path):
        reg = self._registry(tmp_path)
        reg.create_skill("exists", "Desc", "Body.")

        result = reg.create_skill("exists", "Desc2", "Body2.")

        assert result is None

    def test_create_invalidates_discover_cache(self, tmp_path):
        reg = self._registry(tmp_path)
        reg.discover()
        assert reg._cache is not None

        reg.create_skill("cached", "Desc", "Body.")

        assert reg._cache is None
        skills = reg.discover()
        assert any(s.name == "cached" for s in skills)

    def test_create_invalid_name_raises_value_error(self, tmp_path):
        reg = self._registry(tmp_path)

        with pytest.raises(ValueError):
            reg.create_skill("Bad Name", "Desc", "Body.")

    def test_create_name_with_slash_raises_value_error(self, tmp_path):
        reg = self._registry(tmp_path)

        with pytest.raises(ValueError):
            reg.create_skill("foo/bar", "Desc", "Body.")

    def test_create_name_with_dot_dot_raises_value_error(self, tmp_path):
        reg = self._registry(tmp_path)

        with pytest.raises(ValueError):
            reg.create_skill("..", "Desc", "Body.")

    def test_create_name_leading_hyphen_raises_value_error(self, tmp_path):
        reg = self._registry(tmp_path)

        with pytest.raises(ValueError):
            reg.create_skill("-bad", "Desc", "Body.")

    def test_create_name_trailing_hyphen_raises_value_error(self, tmp_path):
        reg = self._registry(tmp_path)

        with pytest.raises(ValueError):
            reg.create_skill("bad-", "Desc", "Body.")

    def test_create_single_char_name_ok(self, tmp_path):
        reg = self._registry(tmp_path)

        path = reg.create_skill("a", "Desc", "Body.")

        assert path is not None
        assert path.exists()

    def test_created_skill_is_parsable_and_enabled(self, tmp_path):
        reg = self._registry(tmp_path)

        reg.create_skill("parseable", "A skill", "# Instructions\nDo things.")

        skill = reg.get("parseable")
        assert skill is not None
        assert skill.meta.enabled is True
        assert skill.meta.description == "A skill"
        assert "Do things." in skill.body

    def test_created_skill_frontmatter_has_no_triggers(self, tmp_path):
        reg = self._registry(tmp_path)

        reg.create_skill("no-triggers", "Desc", "Body.")

        skill = reg.get("no-triggers")
        assert skill is not None
        assert skill.meta.triggers == []


class TestSkillNameRe:
    @pytest.mark.parametrize("name,valid", [
        ("react-patterns", True),
        ("a", True),
        ("a-b", True),
        ("abc123", True),
        ("0", True),
        ("0abc", True),
        ("a" * 64, True),
        ("Bad", False),
        ("bad name", False),
        ("foo/bar", False),
        ("..", False),
        ("-bad", False),
        ("bad-", False),
        ("a" * 65, False),
        ("", False),
        ("has space", False),
        ("has_underscore", False),
    ])
    def test_name_validation(self, name, valid):
        import re
        assert bool(SKILL_NAME_RE.match(name)) == valid
