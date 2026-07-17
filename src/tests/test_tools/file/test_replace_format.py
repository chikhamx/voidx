from pathlib import Path

import pytest

from voidx.tools.base import ToolContext
from voidx.tools.registry import ToolRegistry


class FormattingManager:
    def __init__(self, workspace, formatted_text):
        self.workspace = Path(workspace)
        self.formatted_text = formatted_text
        self.calls = []

    async def formatted_range_text(self, file_path, range_):
        source = (self.workspace / file_path).read_text(encoding="utf-8")
        self.calls.append((range_, source))
        return source != self.formatted_text, source, self.formatted_text


@pytest.mark.asyncio
async def test_replace_formats_resolved_range_and_returns_final_diff(tmp_path):
    target = tmp_path / "sample.py"
    target.write_text("one\ntwo = 2\nthree\n", encoding="utf-8")
    manager = FormattingManager(tmp_path, "one\nTWO = 2\nthree\n")
    ctx = ToolContext(workspace=str(tmp_path), lsp_manager=manager)
    registry = ToolRegistry()
    await registry.execute_tool("read", {"file_path": "sample.py"}, ctx)

    result = await registry.execute_tool(
        "replace",
        {
            "file_path": "sample.py",
            "bounds": [{"line_no": 2, "anchor": "two"}],
            "new_string": "TWO=2",
        },
        ctx,
    )

    assert result.metadata.get("error") is not True
    assert result.metadata["formatting_status"] == "formatted"
    assert manager.calls[0][1] == "one\nTWO=2\nthree\n"
    range_ = manager.calls[0][0]
    assert (range_.start.line, range_.end.line) == (1, 2)
    assert target.read_text(encoding="utf-8") == "one\nTWO = 2\nthree\n"
    assert "+TWO = 2" in result.diff


@pytest.mark.asyncio
async def test_replace_auto_create_formats_new_file(tmp_path):
    manager = FormattingManager(tmp_path, "print(1)\n")
    ctx = ToolContext(workspace=str(tmp_path), lsp_manager=manager)

    result = await ToolRegistry().execute_tool(
        "replace",
        {
            "file_path": "created.py",
            "bounds": [{"line_no": 1, "anchor": "missing"}],
            "new_string": "print( 1 )\n",
        },
        ctx,
    )

    assert result.metadata.get("error") is not True
    assert result.metadata["auto_created"] is True
    assert result.metadata["formatting_status"] == "formatted"
    assert (tmp_path / "created.py").read_text(encoding="utf-8") == "print(1)\n"
