"""Search/Find tools on authorized external paths: absolute POSIX output contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.tool_registry import build_registry
from voidx.tooling.application.execution import AuthorizationRuntime
from voidx.tooling.application.execution import FileToolContext as ToolContext
from voidx.tooling.domain.grants import AccessGrants


def _ctx(workspace: Path, grants: AccessGrants) -> ToolContext:
    return ToolContext(
        workspace=str(workspace),
        authorization_service=AuthorizationRuntime(access_grants_reader=lambda: grants),
    )


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture
def external(tmp_path):
    ext = tmp_path / "external"
    ext.mkdir()
    return ext


@pytest.mark.asyncio
async def test_search_external_granted_dir_returns_absolute_paths(workspace, external):
    (external / "main.py").write_text("def hello():\n    return 42\n", encoding="utf-8")
    sub = external / "pkg"
    sub.mkdir()
    (sub / "mod.py").write_text("hello = 1\n", encoding="utf-8")
    grants = AccessGrants.from_parts(readable_dirs=[str(external.resolve())])
    ctx = _ctx(workspace, grants)

    result = await build_registry().execute_tool(
        "search", {"query": "hello", "path": str(external)}, ctx
    )

    assert result.metadata.get("error") is not True
    data = json.loads(result.output)
    paths = {match["path"] for match in data["matches"]}
    expected = {
        str((external / "main.py").resolve()).replace("\\", "/"),
        str((external / "pkg" / "mod.py").resolve()).replace("\\", "/"),
    }
    assert paths == expected
    for path in paths:
        assert Path(path).is_absolute()


@pytest.mark.asyncio
async def test_find_external_granted_dir_returns_absolute_paths(workspace, external):
    (external / "alpha.py").touch()
    (external / "beta.py").touch()
    grants = AccessGrants.from_parts(readable_dirs=[str(external.resolve())])
    ctx = _ctx(workspace, grants)

    result = await build_registry().execute_tool(
        "find", {"extensions": ["py"], "path": str(external)}, ctx
    )

    assert result.metadata.get("error") is not True
    files = json.loads(result.output)["files"]
    names = {f["name"] for f in files}
    assert names == {"alpha.py", "beta.py"}
    for f in files:
        assert Path(f["path"]).is_absolute()


@pytest.mark.asyncio
async def test_search_external_without_grant_blocked(workspace, external):
    (external / "secret.py").write_text("hello\n", encoding="utf-8")
    ctx = _ctx(workspace, AccessGrants.from_parts())

    result = await build_registry().execute_tool(
        "search", {"query": "hello", "path": str(external)}, ctx
    )

    assert result.metadata.get("error") is True
    assert "Path traversal blocked" in result.output


@pytest.mark.asyncio
async def test_find_external_without_grant_blocked(workspace, external):
    ctx = _ctx(workspace, AccessGrants.from_parts())

    result = await build_registry().execute_tool(
        "find", {"extensions": ["py"], "path": str(external)}, ctx
    )

    assert result.metadata.get("error") is True
    assert "Path traversal blocked" in result.output


@pytest.mark.asyncio
async def test_search_workspace_paths_stay_relative(workspace):
    (workspace / "a.py").write_text("hello\n", encoding="utf-8")
    ctx = _ctx(workspace, AccessGrants.from_parts())

    result = await build_registry().execute_tool("search", {"query": "hello"}, ctx)

    data = json.loads(result.output)
    paths = [match["path"] for match in data["matches"]]
    assert paths == ["a.py"]


@pytest.mark.asyncio
async def test_find_external_respects_scope_gitignore(workspace, external):
    (external / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (external / "ignored.py").touch()
    (external / "visible.py").touch()
    grants = AccessGrants.from_parts(readable_dirs=[str(external.resolve())])
    ctx = _ctx(workspace, grants)

    result = await build_registry().execute_tool(
        "find", {"extensions": ["py"], "path": str(external)}, ctx
    )

    names = {f["name"] for f in json.loads(result.output)["files"]}
    assert names == {"visible.py"}


@pytest.mark.asyncio
async def test_search_external_single_file(workspace, external):
    target = external / "one.py"
    target.write_text("hello world\n", encoding="utf-8")
    grants = AccessGrants.from_parts(readable_files=[str(target.resolve())])
    ctx = _ctx(workspace, grants)

    result = await build_registry().execute_tool(
        "search", {"query": "hello", "path": str(target)}, ctx
    )

    assert result.metadata.get("error") is not True
    data = json.loads(result.output)
    paths = [match["path"] for match in data["matches"]]
    assert paths == [str(target.resolve()).replace("\\", "/")]
