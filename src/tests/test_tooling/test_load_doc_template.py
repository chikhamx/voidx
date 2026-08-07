"""Tests for the built-in document knowledge-base tool."""

from __future__ import annotations

import importlib.resources
from pathlib import Path

import pytest

from voidx.tooling.domain.context import ToolExecutionContext as ToolContext
from voidx.tooling.builtin.document import DocumentInput, DocumentTool


class TestDocumentTool:
    @pytest.fixture
    def ctx(self, tmp_path: Path) -> ToolContext:
        return ToolContext(workspace=str(tmp_path))

    @pytest.mark.asyncio
    async def test_list_root(self, ctx: ToolContext):
        result = await DocumentTool().execute({"action": "list"}, ctx)

        assert result.title == "Document index: /"
        assert "templates/" in result.output
        assert "voidx-guide/" in result.output
        assert result.metadata == {
            "action": "list",
            "path": "",
            "kind": "index",
            "directory": "",
        }

    @pytest.mark.asyncio
    async def test_list_directory(self, ctx: ToolContext):
        result = await DocumentTool().execute(
            {"action": "list", "path": "voidx-guide"}, ctx
        )

        assert result.title == "Document index: voidx-guide"
        assert "voidx-guide/quickstart.md" in result.output
        assert "path | use_when | keywords" in result.output
        assert result.metadata["directory"] == "voidx-guide"

    @pytest.mark.asyncio
    async def test_list_rejects_file_path(self, ctx: ToolContext):
        result = await DocumentTool().execute(
            {"action": "list", "path": "voidx-guide/quickstart.md"}, ctx
        )

        assert result.metadata.get("error") is True
        assert "list requires a directory path" in result.output

    @pytest.mark.asyncio
    async def test_read_template(self, ctx: ToolContext):
        result = await DocumentTool().execute(
            {"action": "read", "path": "templates/prd.md"}, ctx
        )

        assert result.title == "Document: templates/prd.md"
        assert len(result.output) > 50
        assert result.metadata == {
            "action": "read",
            "path": "templates/prd.md",
            "kind": "document",
            "directory": "templates",
        }

    @pytest.mark.asyncio
    async def test_read_guide_section(self, ctx: ToolContext):
        result = await DocumentTool().execute(
            {"action": "read", "path": "voidx-guide/quickstart.md"}, ctx
        )

        assert result.title == "Document: voidx-guide/quickstart.md"
        assert "快速上手" in result.output
        assert "voidx -w /path/to/project" in result.output

    @pytest.mark.asyncio
    async def test_read_requires_path(self, ctx: ToolContext):
        result = await DocumentTool().execute({"action": "read"}, ctx)

        assert result.metadata.get("error") is True
        assert "read requires path" in result.output

    @pytest.mark.asyncio
    async def test_read_requires_markdown_file(self, ctx: ToolContext):
        result = await DocumentTool().execute(
            {"action": "read", "path": "voidx-guide"}, ctx
        )

        assert result.metadata.get("error") is True
        assert "read requires a .md file path" in result.output

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path",
        ["/tmp/a.md", "../secret.md", "a/../b.md", "a//b.md", "a/./b.md", "./a.md"],
    )
    async def test_rejects_unsafe_paths(self, ctx: ToolContext, path: str):
        result = await DocumentTool().execute({"action": "read", "path": path}, ctx)

        assert result.metadata.get("error") is True
        assert "invalid path" in result.output

    @pytest.mark.asyncio
    async def test_reject_backslash(self, ctx: ToolContext):
        result = await DocumentTool().execute(
            {"action": "read", "path": r"voidx-guide\\quickstart.md"}, ctx
        )

        assert result.metadata.get("error") is True
        assert "invalid path" in result.output

    @pytest.mark.asyncio
    async def test_missing_file_points_to_list(self, ctx: ToolContext):
        result = await DocumentTool().execute(
            {"action": "read", "path": "voidx-guide/missing.md"}, ctx
        )

        assert result.metadata.get("error") is True
        assert "document(action=\"list\")" in result.output

    def test_schema_no_doc_type(self):
        schema = DocumentInput.model_json_schema()

        assert "action" in schema["properties"]
        assert "path" in schema["properties"]
        assert "doc_type" not in schema["properties"]

    @pytest.mark.asyncio
    async def test_schema_forbids_extra_fields(self, ctx: ToolContext):
        result = await DocumentTool().execute(
            {"action": "read", "path": "templates/prd.md", "doc_type": "prd"}, ctx
        )

        assert result.metadata.get("error") is True
        assert "Invalid arguments" in result.output

    @pytest.mark.asyncio
    async def test_old_doc_type_not_supported(self, ctx: ToolContext):
        result = await DocumentTool().execute({"doc_type": "prd"}, ctx)

        assert result.metadata.get("error") is True
        assert "Invalid arguments" in result.output

    def test_package_data_contains_documents(self):
        root = importlib.resources.files("voidx.data").joinpath("documents")

        assert root.joinpath("README.md").read_text(encoding="utf-8")
        assert root.joinpath("templates/prd.md").read_text(encoding="utf-8")
        assert root.joinpath("voidx-guide/quickstart.md").read_text(encoding="utf-8")

    def test_all_docs_referenced_in_readme(self):
        root = importlib.resources.files("voidx.data").joinpath("documents")
        directories = [root.joinpath("templates"), root.joinpath("voidx-guide")]

        for directory in directories:
            readme = directory.joinpath("README.md").read_text(encoding="utf-8")
            directory_name = directory.name
            for ref in directory.iterdir():
                if ref.name == "README.md" or ref.name.startswith("__"):
                    continue
                if ref.name.endswith(".md"):
                    assert f"{directory_name}/{ref.name}" in readme
