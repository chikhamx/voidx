"""Tests for bash sed edit routing through file tools."""

from __future__ import annotations

import pytest

from voidx.tools.base import ToolContext
from voidx.tools.registry import ToolRegistry


def _make_ctx(tmp_path, registry: ToolRegistry | None = None) -> ToolContext:
    return ToolContext(
        workspace=str(tmp_path),
        permission_mode="full_access",
        tool_registry=registry,
    )


def _make_registry() -> ToolRegistry:
    return ToolRegistry()


class TestBashSedAutoRoute:
    """sed -i edits route through replace so file management sees the write."""

    @pytest.mark.asyncio
    async def test_macos_sed_line_substitution_routes_to_replace(self, tmp_path):
        target = tmp_path / "code.py"
        target.write_text("keep\nREMOVE_ME = True\nend\n", encoding="utf-8")
        registry = _make_registry()
        ctx = _make_ctx(tmp_path, registry)

        result = await registry.execute_tool(
            "bash",
            {"command": "sed -i '' -e '2s/REMOVE_ME/kept/' code.py"},
            ctx,
        )

        assert result.metadata.get("tool") == "replace"
        assert result.metadata.get("routed_from") == "bash"
        assert result.metadata.get("routed_tool_args") == {
            "file_path": "code.py",
            "bounds": [{"line_no": 2, "anchor": "REMOVE_ME"}],
            "new_string": "kept = True",
        }
        assert target.read_text(encoding="utf-8") == "keep\nkept = True\nend\n"

    @pytest.mark.asyncio
    async def test_macos_sed_line_delete_routes_to_replace(self, tmp_path):
        target = tmp_path / "code.py"
        target.write_text("keep\nREMOVE_ME\nend\n", encoding="utf-8")
        registry = _make_registry()
        ctx = _make_ctx(tmp_path, registry)

        result = await registry.execute_tool(
            "bash",
            {"command": "sed -i '' -e '2d' code.py"},
            ctx,
        )

        assert result.metadata.get("tool") == "replace"
        assert result.metadata.get("routed_from") == "bash"
        assert result.metadata.get("routed_tool_args") == {
            "file_path": "code.py",
            "bounds": [{"line_no": 2, "anchor": "REMOVE_ME"}],
            "new_string": "",
        }
        assert target.read_text(encoding="utf-8") == "keep\nend\n"

    @pytest.mark.asyncio
    async def test_global_sed_substitution_is_hint_only_and_does_not_modify_file(self, tmp_path):
        target = tmp_path / "code.py"
        target.write_text("REMOVE_ME\nREMOVE_ME\n", encoding="utf-8")
        registry = _make_registry()
        ctx = _make_ctx(tmp_path, registry)

        result = await registry.execute_tool(
            "bash",
            {"command": "sed -i '' -e 's/REMOVE_ME/kept/g' code.py"},
            ctx,
        )

        assert result.metadata.get("skipped") is True
        assert result.metadata.get("route_hint", {}).get("tool_id") == "replace"
        assert "routed_from" not in result.metadata
        assert target.read_text(encoding="utf-8") == "REMOVE_ME\nREMOVE_ME\n"

    @pytest.mark.asyncio
    async def test_sed_backup_suffix_is_hint_only_and_does_not_create_backup(self, tmp_path):
        target = tmp_path / "code.py"
        target.write_text("keep\nREMOVE_ME\nend\n", encoding="utf-8")
        registry = _make_registry()
        ctx = _make_ctx(tmp_path, registry)

        result = await registry.execute_tool(
            "bash",
            {"command": "sed -i.bak -e '2d' code.py"},
            ctx,
        )

        assert result.metadata.get("skipped") is True
        assert result.metadata.get("route_hint", {}).get("tool_id") == "replace"
        assert "routed_from" not in result.metadata
        assert target.read_text(encoding="utf-8") == "keep\nREMOVE_ME\nend\n"
        assert not (tmp_path / "code.py.bak").exists()
