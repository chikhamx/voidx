import json

import pytest

from voidx.tools.base import ToolContext
from voidx.tools.search import FindTool, SearchTool
from voidx.tools.registry import ToolRegistry


@pytest.mark.asyncio
async def test_find_uses_name_query_and_extensions(tmp_path):
    (tmp_path / "SearchTool.py").touch()
    (tmp_path / "notes.txt").touch()
    ctx = ToolContext(workspace=str(tmp_path))
    result = await ToolRegistry().execute_tool(
        "find", {"query": "search", "extensions": ["py"], "case": "insensitive"}, ctx
    )
    data = json.loads(result.output)
    assert data["files"] == [{"path": "SearchTool.py", "name": "SearchTool.py"}]
    assert data["truncated"] is False


@pytest.mark.asyncio
async def test_search_is_literal_by_default_and_grouped(tmp_path):
    (tmp_path / "a.py").write_text("x[0] = 1\n")
    ctx = ToolContext(workspace=str(tmp_path))
    result = await ToolRegistry().execute_tool(
        "search", {"query": "x[0]", "extensions": ["py"]}, ctx
    )
    data = json.loads(result.output)
    assert data["matches"][0]["path"] == "a.py"
    assert data["matches"][0]["hits"][0]["column"] == 1
    assert data["matches"][0]["hits"][0]["text"] == "x[0] = 1"


@pytest.mark.asyncio
async def test_find_requires_query_or_extensions(tmp_path):
    ctx = ToolContext(workspace=str(tmp_path))
    result = await ToolRegistry().execute_tool("find", {}, ctx)
    assert result.metadata["error"] is True


@pytest.mark.asyncio
async def test_search_skips_binary_explicit_file(tmp_path):
    binary = tmp_path / "data.bin"
    binary.write_bytes(b"needle\x00value")
    ctx = ToolContext(workspace=str(tmp_path))
    result = await ToolRegistry().execute_tool(
        "search", {"query": "needle", "path": "data.bin"}, ctx
    )
    assert json.loads(result.output)["matches"] == []



def test_search_tool_schemas_are_strict_openai_objects():
    for tool in (FindTool(), SearchTool()):
        schema = tool.parameters_schema()
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
