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


# --- Denied destructive commands: clean flag combination consistency ---

@pytest.mark.asyncio
async def test_git_clean_fd_separate_flags_denied(tmp_path):
    """clean -f -d (separate flags, same effect as -fd) must be denied."""
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _run(repo, "add", "f.txt")
    _run(repo, "commit", "-m", "init")
    (repo / "junk.txt").write_text("junk\n", encoding="utf-8")

    result = await GitTool().execute(
        {"args": "clean -f -d"},
        ToolContext(workspace=str(repo)),
    )
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"].startswith("command_denied")


@pytest.mark.asyncio
async def test_git_clean_fd_combined_denied(tmp_path):
    """clean -fd (combined flag) must be denied."""
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _run(repo, "add", "f.txt")
    _run(repo, "commit", "-m", "init")
    (repo / "junk.txt").write_text("junk\n", encoding="utf-8")

    result = await GitTool().execute(
        {"args": "clean -fd"},
        ToolContext(workspace=str(repo)),
    )
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"].startswith("command_denied")



# --- Summary format (UI display) ---


def test_git_summary_ok_no_command_prefix():
    """Summary should not duplicate the command name shown in the tool header."""
    from voidx.tools.git import _result

    ctx = ToolContext(workspace=".")
    r = _result("log", ctx, ok=True)
    assert r.summary == "ok"


def test_git_summary_failed_no_command_prefix():
    from voidx.tools.git import _result

    ctx = ToolContext(workspace=".")
    r = _result("push", ctx, ok=False, error="command_denied")
    assert r.summary == "failed"
