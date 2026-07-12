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


# --- Raw output: add/commit ---

@pytest.mark.asyncio
async def test_git_add_raw(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")

    result = await GitTool().execute(
        {"args": "add f.txt"},
        ToolContext(workspace=str(repo)),
    )
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["data"]["returncode"] == 0


@pytest.mark.asyncio
async def test_git_commit_raw(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _run(repo, "add", "f.txt")

    result = await GitTool().execute(
        {"args": "commit -m initial"},
        ToolContext(workspace=str(repo)),
    )
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["data"]["returncode"] == 0


# --- Raw output: rev-parse ---

@pytest.mark.asyncio
async def test_git_rev_parse_raw(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _run(repo, "add", "f.txt")
    _run(repo, "commit", "-m", "init")

    result = await GitTool().execute(
        {"args": "rev-parse HEAD"},
        ToolContext(workspace=str(repo)),
    )
    payload = _payload(result)
    assert payload["ok"] is True
    assert len(payload["data"]["stdout"].strip()) == 40


# --- Raw output: ls-files ---

@pytest.mark.asyncio
async def test_git_ls_files_raw(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "a.txt").write_text("x\n", encoding="utf-8")
    (repo / "b.txt").write_text("y\n", encoding="utf-8")
    _run(repo, "add", "a.txt", "b.txt")

    result = await GitTool().execute(
        {"args": "ls-files"},
        ToolContext(workspace=str(repo)),
    )
    payload = _payload(result)
    assert payload["ok"] is True
    assert "a.txt" in payload["data"]["stdout"]
    assert "b.txt" in payload["data"]["stdout"]


# --- Permission classification ---

def test_permission_read_only_status():
    from voidx.permission.rules import _is_read_only_git_tool_command
    assert _is_read_only_git_tool_command({"args": "status"}) is True
    assert _is_read_only_git_tool_command({"args": "log --oneline"}) is True
    assert _is_read_only_git_tool_command({"args": "diff"}) is True
    assert _is_read_only_git_tool_command({"command": "status --porcelain"}) is True
    assert _is_read_only_git_tool_command({"command": "log --oneline -10 -- src/app.py"}) is True


def test_permission_write_add():
    from voidx.permission.rules import _is_read_only_git_tool_command
    assert _is_read_only_git_tool_command({"args": "add file.txt"}) is False
    assert _is_read_only_git_tool_command({"args": "commit -m x"}) is False
    assert _is_read_only_git_tool_command({"args": "push"}) is False


def test_permission_branch_read_vs_write():
    from voidx.permission.rules import _is_read_only_git_tool_command
    assert _is_read_only_git_tool_command({"args": "branch"}) is True
    assert _is_read_only_git_tool_command({"args": "branch -d feature"}) is False
    assert _is_read_only_git_tool_command({"args": "branch -D feature"}) is False


def test_permission_tag_read_vs_write():
    from voidx.permission.rules import _is_read_only_git_tool_command
    assert _is_read_only_git_tool_command({"args": "tag"}) is True
    assert _is_read_only_git_tool_command({"args": "tag -d v1.0"}) is False


def test_permission_stash_read_vs_write():
    from voidx.permission.rules import _is_read_only_git_tool_command
    assert _is_read_only_git_tool_command({"args": "stash list"}) is True
    assert _is_read_only_git_tool_command({"args": "stash push"}) is False
    assert _is_read_only_git_tool_command({"args": "stash pop"}) is False


def test_permission_checkout_is_write():
    from voidx.permission.rules import _is_read_only_git_tool_command
    assert _is_read_only_git_tool_command({"args": "checkout main"}) is False


def test_permission_remote_read_vs_write():
    from voidx.permission.rules import _is_read_only_git_tool_command
    assert _is_read_only_git_tool_command({"args": "remote -v"}) is True
    assert _is_read_only_git_tool_command({"args": "remote add origin url"}) is False


def test_permission_reflog_read_vs_write():
    from voidx.permission.rules import _is_read_only_git_tool_command
    assert _is_read_only_git_tool_command({"args": "reflog show"}) is True
    assert _is_read_only_git_tool_command({"args": "reflog list"}) is True
    assert _is_read_only_git_tool_command({"args": "reflog expire --all"}) is False


def test_git_is_read_only_reflog():
    from voidx.tools.git import _is_read_only_subcommand
    assert _is_read_only_subcommand("reflog", ["show"]) is True
    assert _is_read_only_subcommand("reflog", ["list"]) is True
    assert _is_read_only_subcommand("reflog", ["expire", "--all"]) is False


def test_git_is_read_only_public_api():
    from voidx.tools.git import is_git_read_only
    assert is_git_read_only({"args": "status"}) is True
    assert is_git_read_only({"args": "commit -m x"}) is False
    assert is_git_read_only({"args": "reflog show"}) is True


# --- Bash hint ---

def test_bash_hint_git_status():
    from voidx.tools.bash.hint.git import _hint_git
    hint = _hint_git("git status --porcelain", ["git", "status", "--porcelain"])
    assert hint is not None
    assert hint.tool_id == "git"
    assert "status" in hint.llm_hint


def test_bash_hint_git_push_now_hintable():
    from voidx.tools.bash.hint.git import _hint_git
    hint = _hint_git("git push", ["git", "push"])
    assert hint is not None
    assert hint.tool_id == "git"


# --- Denied destructive commands ---

@pytest.mark.asyncio
async def test_git_reset_hard_denied(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _run(repo, "add", "f.txt")
    _run(repo, "commit", "-m", "init")
    (repo / "f.txt").write_text("y\n", encoding="utf-8")

    result = await GitTool().execute(
        {"args": "reset --hard HEAD"},
        ToolContext(workspace=str(repo)),
    )
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"].startswith("command_denied")


@pytest.mark.asyncio
async def test_git_reset_soft_allowed(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _run(repo, "add", "f.txt")
    _run(repo, "commit", "-m", "init")
    (repo / "f.txt").write_text("y\n", encoding="utf-8")
    _run(repo, "add", "f.txt")

    result = await GitTool().execute(
        {"args": "reset --soft HEAD"},
        ToolContext(workspace=str(repo)),
    )
    payload = _payload(result)
    assert payload["ok"] is True


@pytest.mark.asyncio
async def test_git_clean_force_denied(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _run(repo, "add", "f.txt")
    _run(repo, "commit", "-m", "init")
    (repo / "junk.txt").write_text("junk\n", encoding="utf-8")

    result = await GitTool().execute(
        {"args": "clean -fdx"},
        ToolContext(workspace=str(repo)),
    )
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"].startswith("command_denied")


@pytest.mark.asyncio
async def test_git_reflog_expire_denied(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _run(repo, "add", "f.txt")
    _run(repo, "commit", "-m", "init")

    result = await GitTool().execute(
        {"args": "reflog expire --all"},
        ToolContext(workspace=str(repo)),
    )
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"].startswith("command_denied")


@pytest.mark.asyncio
async def test_git_reflog_show_allowed(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _run(repo, "add", "f.txt")
    _run(repo, "commit", "-m", "init")

    result = await GitTool().execute(
        {"args": "reflog show"},
        ToolContext(workspace=str(repo)),
    )
    payload = _payload(result)
    assert payload["ok"] is True


# --- Pathspec safety for raw write commands ---

@pytest.mark.asyncio
async def test_git_add_pathspec_escape_rejected(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")

    result = await GitTool().execute(
        {"args": f"add -- ../outside.txt"},
        ToolContext(workspace=str(repo)),
    )
    payload = _payload(result)
    assert payload["ok"] is False
    assert "outside" in payload["error"].lower() or "workspace" in payload["error"].lower() or "path" in payload["error"].lower()


@pytest.mark.asyncio
async def test_git_add_pathspec_within_workspace_ok(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")

    result = await GitTool().execute(
        {"args": "add -- f.txt"},
        ToolContext(workspace=str(repo)),
    )
    payload = _payload(result)
    assert payload["ok"] is True


@pytest.mark.asyncio
async def test_git_add_pathspec_escape_without_separator_rejected(tmp_path):
    """git add ../outside.txt (no --) must also be rejected."""
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")

    result = await GitTool().execute(
        {"args": "add ../outside.txt"},
        ToolContext(workspace=str(repo)),
    )
    payload = _payload(result)
    assert payload["ok"] is False
    assert "outside" in payload["error"].lower() or "workspace" in payload["error"].lower() or "path" in payload["error"].lower()


@pytest.mark.asyncio
async def test_git_add_pathspec_without_separator_within_workspace_ok(tmp_path):
    """git add f.txt (no --) should work for paths inside workspace."""
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")

    result = await GitTool().execute(
        {"args": "add f.txt"},
        ToolContext(workspace=str(repo)),
    )
    payload = _payload(result)
    assert payload["ok"] is True


@pytest.mark.asyncio
async def test_git_restore_pathspec_escape_without_separator_rejected(tmp_path):
    """git restore ../outside.txt (no --) must be rejected."""
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _run(repo, "add", "f.txt")
    _run(repo, "commit", "-m", "init")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")

    result = await GitTool().execute(
        {"args": "restore ../outside.txt"},
        ToolContext(workspace=str(repo)),
    )
    payload = _payload(result)
    assert payload["ok"] is False

# --- Permission: reflog classification ---

def test_permission_reflog_show_is_read():
    from voidx.permission.rules import _is_read_only_git_tool_command
    assert _is_read_only_git_tool_command({"args": "reflog show"}) is True
    assert _is_read_only_git_tool_command({"args": "reflog list"}) is True


def test_permission_reflog_expire_is_write():
    from voidx.permission.rules import _is_read_only_git_tool_command
    assert _is_read_only_git_tool_command({"args": "reflog expire --all"}) is False


def test_permission_config_get_is_read():
    from voidx.permission.rules import _is_read_only_git_tool_command
    assert _is_read_only_git_tool_command({"args": "config --get user.name"}) is True


def test_permission_config_set_is_write():
    from voidx.permission.rules import _is_read_only_git_tool_command
    assert _is_read_only_git_tool_command({"args": "config user.name foo"}) is False


def test_permission_config_global_read_is_read():
    """config --global user.name (read) should be classified as read-only."""
    from voidx.permission.rules import _is_read_only_git_tool_command
    assert _is_read_only_git_tool_command({"args": "config --global user.name"}) is True
    assert _is_read_only_git_tool_command({"args": "config --system user.name"}) is True
    assert _is_read_only_git_tool_command({"args": "config --local user.name"}) is True


def test_permission_config_global_set_is_write():
    """config --global user.name foo (set) should be classified as write."""
    from voidx.permission.rules import _is_read_only_git_tool_command
    assert _is_read_only_git_tool_command({"args": "config --global user.name foo"}) is False


