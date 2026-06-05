"""Tests for load_doc_template tool."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest

from voidx.tools.base import ToolContext
from voidx.tools.doc_template import LoadDocTemplateTool, LoadDocTemplateInput, _DOC_TYPES


@pytest.fixture
def tool():
    return LoadDocTemplateTool()


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(workspace=str(tmp_path))


def _run(coro):
    """Run async coroutine, compatible with both standalone and pytest-asyncio contexts."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


class TestLoadDocTemplateSchema:
    def test_input_validates_doc_type(self):
        inp = LoadDocTemplateInput(doc_type="prd")
        assert inp.doc_type == "prd"

    def test_tool_id_and_description(self):
        tool = LoadDocTemplateTool()
        assert tool.id == "load_doc_template"
        assert "template" in tool.description.lower()


class TestLoadDocTemplateExecution:
    @pytest.mark.parametrize("doc_type", _DOC_TYPES)
    def test_loads_each_template(self, tool, ctx, doc_type):
        result = _run(tool.execute({"doc_type": doc_type}, ctx))
        assert result.metadata.get("error") is None
        assert len(result.output) > 50
        assert result.metadata["doc_type"] == doc_type
        assert result.metadata["source"] == "bundled"

    def test_unknown_doc_type_returns_error(self, tool, ctx):
        result = _run(tool.execute({"doc_type": "nonexistent"}, ctx))
        assert result.metadata.get("error") is True
        assert "Unknown" in result.output
        for dt in _DOC_TYPES:
            assert dt in result.output

    def test_prd_template_has_key_sections(self, tool, ctx):
        result = _run(tool.execute({"doc_type": "prd"}, ctx))
        assert result.metadata.get("error") is None
        assert "定位" in result.output or "Positioning" in result.output
        assert "功能清单" in result.output or "Feature" in result.output
        assert "交互细节" in result.output or "Interaction" in result.output
        assert "状态清单" in result.output or "State" in result.output
        assert "数据规范" in result.output or "Data" in result.output
        assert "文案规范" in result.output or "Copy" in result.output

    def test_tech_design_template_has_key_sections(self, tool, ctx):
        result = _run(tool.execute({"doc_type": "tech-design"}, ctx))
        assert result.metadata.get("error") is None
        assert "Architecture" in result.output
        assert "Data Model" in result.output
        assert "API Contract" in result.output
        assert "Error Handling" in result.output

    def test_template_has_placeholders(self, tool, ctx):
        result = _run(tool.execute({"doc_type": "prd"}, ctx))
        assert "{" in result.output
        assert "}" in result.output

    def test_template_has_guidance_comments(self, tool, ctx):
        result = _run(tool.execute({"doc_type": "prd"}, ctx))
        assert "<!--" in result.output

    def test_project_override_takes_priority(self, tool, tmp_path):
        ctx = ToolContext(workspace=str(tmp_path))
        override_dir = tmp_path / ".voidx" / "templates"
        override_dir.mkdir(parents=True)
        (override_dir / "prd.md").write_text("---\nname: prd\n---\nCustom PRD template")
        result = _run(tool.execute({"doc_type": "prd"}, ctx))
        assert result.metadata.get("error") is None
        assert "Custom PRD template" in result.output
        assert result.metadata["source"] == "project"

    def test_global_override_takes_priority_over_bundled(self, tool, tmp_path, monkeypatch):
        ctx = ToolContext(workspace=str(tmp_path))
        global_dir = tmp_path / "home" / ".voidx" / "templates"
        global_dir.mkdir(parents=True)
        (global_dir / "rfc.md").write_text("---\nname: rfc\n---\nGlobal RFC template")
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
        result = _run(tool.execute({"doc_type": "rfc"}, ctx))
        assert result.metadata.get("error") is None
        assert "Global RFC template" in result.output
        assert result.metadata["source"] == "global"

    def test_project_override_beats_global(self, tool, tmp_path, monkeypatch):
        ctx = ToolContext(workspace=str(tmp_path))
        global_dir = tmp_path / "home" / ".voidx" / "templates"
        global_dir.mkdir(parents=True)
        (global_dir / "api-doc.md").write_text("---\nname: api-doc\n---\nGlobal API doc")
        project_dir = tmp_path / ".voidx" / "templates"
        project_dir.mkdir(parents=True)
        (project_dir / "api-doc.md").write_text("---\nname: api-doc\n---\nProject API doc")
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
        result = _run(tool.execute({"doc_type": "api-doc"}, ctx))
        assert result.metadata.get("error") is None
        assert "Project API doc" in result.output
        assert result.metadata["source"] == "project"

    def test_display_name_extracted_from_frontmatter(self, tool, ctx):
        result = _run(tool.execute({"doc_type": "prd"}, ctx))
        assert result.metadata["display_name"] == "Product Requirements Doc"

    def test_title_includes_source(self, tool, ctx):
        result = _run(tool.execute({"doc_type": "tech-design"}, ctx))
        assert "bundled" in result.title


class TestLoadDocTemplateRegistry:
    def test_registered_in_tool_registry(self):
        from voidx.tools.registry import ToolRegistry
        r = ToolRegistry()
        assert "load_doc_template" in r.ids()

    def test_registry_returns_tool_instance(self):
        from voidx.tools.registry import ToolRegistry
        r = ToolRegistry()
        tool = r.get("load_doc_template")
        assert tool is not None
        assert tool.id == "load_doc_template"
