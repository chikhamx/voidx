import json
import subprocess
import sys
from pathlib import Path

import pytest


from voidx.tools import git as git_mod
from voidx.tools.base import ToolContext
from voidx.tools.git import GitTool


def _run(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout


def _init_repo(path: Path) -> Path:
    path.mkdir()
    _run(path, "init")
    _run(path, "config", "user.email", "voidx@example.com")
    _run(path, "config", "user.name", "VoidX Tests")
    return path


def _payload(result):
    return json.loads(result.output)


# --- Schema tests ---

def test_git_schema_has_only_path_and_args():
    schema = GitTool().parameters_schema()
    props = schema["properties"]
    assert set(props.keys()) == {"path", "args"}
    assert props["args"]["type"] == "string"
    assert props["path"]["type"] == "string"


def test_git_tool_descriptions_explain_path_scoped_json_output():
    schema = GitTool().parameters_schema()

    assert "raw git subcommand" in schema["properties"]["args"]["description"]
    assert "Do not include git" in schema["properties"]["args"]["description"]
    assert "working directory" in schema["properties"]["path"]["description"]
    assert "structured JSON" in GitTool.description
    assert "raw stdout/stderr inside JSON" in GitTool.description

@pytest.mark.asyncio
async def test_git_execute_accepts_command_alias(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _run(repo, "add", "f.txt")
    _run(repo, "commit", "-m", "init")

    result = await GitTool().execute(
        {"command": "status --short"},
        ToolContext(workspace=str(repo)),
    )
    payload = _payload(result)

    assert payload["ok"] is True
    assert result.metadata["ok"] is True


@pytest.mark.asyncio
async def test_git_path_overrides_workspace(tmp_path):
    """path field should scope git execution to a subdirectory within workspace."""
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _run(repo, "add", "f.txt")
    _run(repo, "commit", "-m", "init")

    result = await GitTool().execute(
        {"path": ".", "args": "status"},
        ToolContext(workspace=str(repo)),
    )
    payload = _payload(result)
    assert payload["ok"] is True


@pytest.mark.asyncio
async def test_git_path_empty_defaults_to_workspace(tmp_path):
    """Empty path should use workspace root (existing behavior)."""
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _run(repo, "add", "f.txt")
    _run(repo, "commit", "-m", "init")

    result = await GitTool().execute(
        {"args": "status"},
        ToolContext(workspace=str(repo)),
    )
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["workspace"] == str(repo.resolve())


# --- Error handling tests ---

@pytest.mark.asyncio
async def test_git_success_metadata_no_error_key(tmp_path):
    """E2: successful results should not have 'error' in metadata."""
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _run(repo, "add", "f.txt")
    _run(repo, "commit", "-m", "init")

    result = await GitTool().execute(
        {"args": "status"},
        ToolContext(workspace=str(repo)),
    )
    assert result.metadata["ok"] is True
    assert "error" not in result.metadata


@pytest.mark.asyncio
async def test_git_failure_metadata_error_true(tmp_path):
    """E2: failed results should have metadata['error'] = True (bool, not string)."""
    result = await GitTool().execute(
        {"args": "status"},
        ToolContext(workspace=str(tmp_path)),
    )
    assert result.metadata["ok"] is False
    assert result.metadata["error"] is True


@pytest.mark.asyncio
async def test_git_non_repo_returns_structured_error(tmp_path):
    result = await GitTool().execute(
        {"args": "status"},
        ToolContext(workspace=str(tmp_path)),
    )
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"] == "not_a_git_repository"


@pytest.mark.asyncio
async def test_git_empty_args_returns_error(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    result = await GitTool().execute(
        {"args": ""},
        ToolContext(workspace=str(repo)),
    )
    payload = _payload(result)
    assert payload["ok"] is False
    assert "invalid_args" in payload["error"]


@pytest.mark.asyncio
async def test_git_denied_subcommand(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    result = await GitTool().execute(
        {"args": "filter-branch --tree-filter 'rm -f x' HEAD"},
        ToolContext(workspace=str(repo)),
    )
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"].startswith("command_denied")

