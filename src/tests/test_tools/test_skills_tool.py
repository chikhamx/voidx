"""Tests for SkillsTool (op=load/create/list) — TDD red phase."""

import asyncio
import sys
from pathlib import Path


import pytest

from voidx.tools.base import ToolContext
from voidx.skills.context import SKILL_TOOL_CONTEXT_MARKER
from voidx.tools.skills import SkillsTool, SkillsInput


class TestSkillsToolLoad:
    def _write_skill(self, workspace: Path, dirname: str, text: str) -> None:
        skill_dir = workspace / ".voidx" / "skills" / dirname
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")

    @pytest.mark.asyncio
    async def test_load_single_skill(self, tmp_path):
        self._write_skill(
            tmp_path, "docs",
            "---\nname: docs\ndescription: Write docs\n---\nDocs body",
        )

        result = await SkillsTool().execute(
            {"op": "load", "name": "docs"},
            ToolContext(workspace=str(tmp_path)),
        )

        assert result.metadata["count"] == 1
        assert result.metadata["loaded_skills"][0]["name"] == "docs"
        assert result.metadata["loaded_skills"][0]["scope"] == "project"
        assert SKILL_TOOL_CONTEXT_MARKER in result.output
        assert "## Skill: docs" in result.output
        assert "Docs body" in result.output

    @pytest.mark.asyncio
    async def test_load_missing_skill_returns_error_with_available(self, tmp_path):
        self._write_skill(
            tmp_path, "exists",
            "---\nname: exists\ndescription: Exists\n---\nBody",
        )

        result = await SkillsTool().execute(
            {"op": "load", "name": "nope"},
            ToolContext(workspace=str(tmp_path)),
        )

        assert result.metadata["error"] is True
        assert result.metadata["missing"] == ["nope"]
        assert "exists" in result.output

    @pytest.mark.asyncio
    async def test_load_disabled_skill_returns_error(self, tmp_path):
        self._write_skill(
            tmp_path, "disabled",
            "---\nname: disabled\nenabled: false\n---\nBody",
        )

        result = await SkillsTool().execute(
            {"op": "load", "name": "disabled"},
            ToolContext(workspace=str(tmp_path)),
        )

        assert result.metadata["error"] is True
        assert result.metadata["disabled"] == ["disabled"]

    @pytest.mark.asyncio
    async def test_load_rejects_path_input(self, tmp_path):
        result = await SkillsTool().execute(
            {"op": "load", "name": ".voidx/skills/docs/SKILL.md"},
            ToolContext(workspace=str(tmp_path)),
        )

        assert result.metadata["error"] is True
        assert result.metadata["invalid"] == [".voidx/skills/docs/skill.md"]


    @pytest.mark.asyncio
    async def test_load_empty_name_returns_missing_not_invalid(self, tmp_path):
        result = await SkillsTool().execute(
            {"op": "load", "name": ""},
            ToolContext(workspace=str(tmp_path)),
        )

        assert result.metadata["error"] is True
        assert result.metadata["invalid"] == []
        assert result.metadata["missing"] == [""]

class TestSkillsToolCreate:
    @pytest.mark.asyncio
    async def test_create_project_skill(self, tmp_path):
        result = await SkillsTool().execute(
            {"op": "create", "name": "react-patterns", "description": "React patterns", "body": "Use hooks."},
            ToolContext(workspace=str(tmp_path)),
        )

        assert result.metadata["name"] == "react-patterns"
        assert result.metadata["scope"] == "project"
        path = Path(result.metadata["path"])
        assert path.exists()
        text = path.read_text(encoding="utf-8")
        assert "name: react-patterns" in text
        assert "description: React patterns" in text
        assert "enabled: true" in text
        assert "Use hooks." in text

    @pytest.mark.asyncio
    async def test_create_global_skill(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        (tmp_path / "home").mkdir()

        result = await SkillsTool().execute(
            {"op": "create", "name": "global-helper", "description": "Helper", "body": "Body.", "scope": "global"},
            ToolContext(workspace=str(tmp_path)),
        )

        assert result.metadata["scope"] == "global"
        path = Path(result.metadata["path"])
        assert path.exists()

    @pytest.mark.asyncio
    async def test_create_already_exists_returns_hint(self, tmp_path):
        skill_dir = tmp_path / ".voidx" / "skills" / "exists"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: exists\n---\nOld body", encoding="utf-8")

        result = await SkillsTool().execute(
            {"op": "create", "name": "exists", "description": "New", "body": "New body"},
            ToolContext(workspace=str(tmp_path)),
        )

        assert result.metadata.get("error") is not True
        assert "exists" in result.output
        assert "already exists" in result.output.lower() or "已存在" in result.output

    @pytest.mark.asyncio
    async def test_create_invalid_name_returns_error(self, tmp_path):
        result = await SkillsTool().execute(
            {"op": "create", "name": "Bad Name", "description": "Desc", "body": "Body"},
            ToolContext(workspace=str(tmp_path)),
        )

        assert result.metadata["error"] is True

    @pytest.mark.asyncio
    async def test_create_output_mentions_hash_reference(self, tmp_path):
        result = await SkillsTool().execute(
            {"op": "create", "name": "my-skill", "description": "Desc", "body": "Body"},
            ToolContext(workspace=str(tmp_path)),
        )

        assert "#my-skill" in result.output


    @pytest.mark.asyncio
    async def test_create_os_error_returns_friendly_message(self, tmp_path, monkeypatch):
        from voidx.tools.skills import SkillsTool as _ST

        tool = _ST()
        original = _ST._registry_for

        class _BadRegistry:
            project_dir = tmp_path / "project"
            global_dir = tmp_path / "global"

            def create_skill(self, *args, **kwargs):
                raise OSError("Permission denied")

        tool._registry_for = lambda ws: _BadRegistry()

        result = await tool.execute(
            {"op": "create", "name": "fail-skill", "description": "Desc", "body": "Body"},
            ToolContext(workspace=str(tmp_path)),
        )

        assert result.metadata["error"] is True
        assert "fail-skill" in result.output or "Permission denied" in result.output

class TestSkillsToolList:
    def _write_skill(self, workspace: Path, dirname: str, text: str) -> None:
        skill_dir = workspace / ".voidx" / "skills" / dirname
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")

    @pytest.mark.asyncio
    async def test_list_returns_structured_metadata(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        self._write_skill(
            tmp_path, "docs",
            "---\nname: docs\ndescription: Write docs\n---\nBody",
        )
        self._write_skill(
            tmp_path, "lint",
            "---\nname: lint\ndescription: Lint code\nenabled: false\n---\nBody",
        )

        result = await SkillsTool().execute(
            {"op": "list"},
            ToolContext(workspace=str(tmp_path)),
        )

        assert result.metadata["count"] == 2
        skills = result.metadata["skills"]
        names = [s["name"] for s in skills]
        assert "docs" in names
        assert "lint" in names
        docs = next(s for s in skills if s["name"] == "docs")
        assert docs["scope"] == "project"
        assert docs["enabled"] is True
        assert docs["description"] == "Write docs"
        lint = next(s for s in skills if s["name"] == "lint")
        assert lint["enabled"] is False

    @pytest.mark.asyncio
    async def test_list_empty_returns_count_zero(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        result = await SkillsTool().execute(
            {"op": "list"},
            ToolContext(workspace=str(tmp_path)),
        )

        assert result.metadata["count"] == 0
        assert result.metadata["skills"] == []


class TestSkillsToolSchema:
    def test_parameters_schema_has_op_field(self):
        schema = SkillsTool().parameters_schema()
        assert "op" in schema["properties"]
        assert "load" in schema["properties"]["op"]["enum"]
        assert "create" in schema["properties"]["op"]["enum"]
        assert "list" in schema["properties"]["op"]["enum"]

    def test_parameters_schema_has_all_fields(self):
        schema = SkillsTool().parameters_schema()
        props = schema["properties"]
        assert "name" in props
        assert "description" in props
        assert "body" in props
        assert "scope" in props
