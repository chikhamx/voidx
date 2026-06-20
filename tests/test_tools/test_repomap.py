import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from voidx.tools.base import ToolContext
from voidx.tools.repomap import RepoMapTool


@pytest.mark.asyncio
async def test_repo_map_sandbox_extra_paths_no_crash(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("def hello(): pass\n", encoding="utf-8")

    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "lib.py").write_text("class Lib: pass\n", encoding="utf-8")

    tool = RepoMapTool()
    result = await tool.execute(
        {"path": str(extra)},
        ToolContext(workspace=str(workspace), sandbox_extra_paths=[str(extra)]),
    )

    assert "lib.py" in result.output
    assert "Lib" in result.output


@pytest.mark.asyncio
async def test_repo_map_workspace_relative_paths(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("def hello(): pass\n", encoding="utf-8")

    tool = RepoMapTool()
    result = await tool.execute(
        {},
        ToolContext(workspace=str(workspace)),
    )

    assert "app.py" in result.output
    assert "hello" in result.output
