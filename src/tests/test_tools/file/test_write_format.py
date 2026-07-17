import pytest

from voidx.tools.base import ToolContext
from voidx.tools.registry import ToolRegistry


class FormattingManager:
    def __init__(self, workspace, formatted_text):
        self.workspace = workspace
        self.formatted_text = formatted_text
        self.calls = []

    async def formatted_range_text(self, file_path, range_):
        path = __import__("pathlib").Path(self.workspace) / file_path
        source = path.read_text(encoding="utf-8")
        self.calls.append((file_path, range_, source))
        return self.formatted_text != source, source, self.formatted_text


@pytest.mark.asyncio
async def test_write_insert_formats_actual_edited_range_and_returns_final_diff(tmp_path):
    target = tmp_path / "sample.py"
    target.write_text("one\nthree\n", encoding="utf-8")
    manager = FormattingManager(tmp_path, "one\nTWO\nthree\n")
    ctx = ToolContext(workspace=str(tmp_path), lsp_manager=manager)
    registry = ToolRegistry()
    await registry.execute_tool("read", {"file_path": "sample.py"}, ctx)

    result = await registry.execute_tool(
        "write",
        {"file_path": "sample.py", "op": "insert", "lineno": 2, "new_string": "two\n"},
        ctx,
    )

    assert result.metadata.get("error") is not True
    assert result.metadata["formatting_status"] == "formatted"
    assert target.read_text(encoding="utf-8") == "one\nTWO\nthree\n"
    assert manager.calls[0][2] == "one\ntwo\nthree\n"
    range_ = manager.calls[0][1]
    assert (range_.start.line, range_.end.line) == (1, 2)
    assert "+TWO" in result.diff
    assert "+two" not in result.diff


@pytest.mark.asyncio
async def test_write_full_create_formats_and_failure_preserves_edit(tmp_path):
    class FailingManager:
        async def formatted_range_text(self, file_path, range_):
            raise RuntimeError("broken formatter")

    registry = ToolRegistry()
    success_ctx = ToolContext(
        workspace=str(tmp_path),
        lsp_manager=FormattingManager(tmp_path, "print(1)\n"),
    )
    success = await registry.execute_tool(
        "write",
        {"file_path": "created.py", "op": "write", "new_string": "print( 1 )\n"},
        success_ctx,
    )

    assert success.metadata["formatting_status"] == "formatted"
    assert (tmp_path / "created.py").read_text(encoding="utf-8") == "print(1)\n"

    failed_ctx = ToolContext(workspace=str(tmp_path), lsp_manager=FailingManager())
    failed = await registry.execute_tool(
        "write",
        {"file_path": "failed.py", "op": "write", "new_string": "print( 2 )\n"},
        failed_ctx,
    )

    assert failed.metadata.get("error") is not True
    assert failed.metadata["formatting_status"] == "failed"
    assert (tmp_path / "failed.py").read_text(encoding="utf-8") == "print( 2 )\n"


@pytest.mark.asyncio
async def test_write_skips_formatting_when_disabled(tmp_path):
    target = tmp_path / "disabled.py"
    target.write_text("one\n", encoding="utf-8")
    manager = FormattingManager(tmp_path, "formatted\n")
    ctx = ToolContext(
        workspace=str(tmp_path),
        lsp_manager=manager,
        format_after_edit_enabled=False,
    )
    registry = ToolRegistry()
    await registry.execute_tool("read", {"file_path": "disabled.py"}, ctx)

    result = await registry.execute_tool(
        "write",
        {"file_path": "disabled.py", "op": "append", "new_string": "two\n"},
        ctx,
    )

    assert result.metadata["formatting_status"] == "disabled"
    assert manager.calls == []
    assert target.read_text(encoding="utf-8") == "one\ntwo\n"
