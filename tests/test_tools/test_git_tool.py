import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

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
    assert payload["error"] == "command_denied"


# --- Structured output: status ---

@pytest.mark.asyncio
async def test_git_status_structured(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("old\n", encoding="utf-8")
    _run(repo, "add", "tracked.txt")
    _run(repo, "commit", "-m", "initial")
    (repo / "tracked.txt").write_text("new\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("hello\n", encoding="utf-8")

    result = await GitTool().execute(
        {"args": "status"},
        ToolContext(workspace=str(repo)),
    )
    entries = _payload(result)["data"]["entries"]
    by_path = {entry["path"]: entry for entry in entries}
    assert by_path["tracked.txt"]["unstaged"] == "modified"
    assert by_path["untracked.txt"]["untracked"] is True


@pytest.mark.asyncio
async def test_git_status_returns_branch(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _run(repo, "add", "f.txt")
    _run(repo, "commit", "-m", "init")

    result = await GitTool().execute(
        {"args": "status"},
        ToolContext(workspace=str(repo)),
    )
    payload = _payload(result)
    assert payload["data"]["branch"]


@pytest.mark.asyncio
async def test_git_status_with_pathspec(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "a.txt").write_text("x\n", encoding="utf-8")
    (repo / "b.txt").write_text("y\n", encoding="utf-8")
    _run(repo, "add", "a.txt", "b.txt")

    result = await GitTool().execute(
        {"args": "status -- a.txt"},
        ToolContext(workspace=str(repo)),
    )
    entries = _payload(result)["data"]["entries"]
    paths = {e["path"] for e in entries}
    assert "a.txt" in paths
    assert "b.txt" not in paths


# --- Structured output: diff ---

@pytest.mark.asyncio
async def test_git_diff_structured(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('old')\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")
    (repo / "app.py").write_text("print('new')\n", encoding="utf-8")

    result = await GitTool().execute(
        {"args": "diff -- app.py"},
        ToolContext(workspace=str(repo)),
    )
    entries = _payload(result)["data"]["entries"]
    assert entries[0]["path"] == "app.py"
    assert entries[0]["additions"] == 1
    assert entries[0]["deletions"] == 1
    assert any("print('new')" in hunk for hunk in entries[0]["hunks"])


@pytest.mark.asyncio
async def test_git_diff_cached(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("old\n", encoding="utf-8")
    _run(repo, "add", "f.txt")
    _run(repo, "commit", "-m", "init")
    (repo / "f.txt").write_text("new\n", encoding="utf-8")
    _run(repo, "add", "f.txt")

    result = await GitTool().execute(
        {"args": "diff --cached"},
        ToolContext(workspace=str(repo)),
    )
    entries = _payload(result)["data"]["entries"]
    assert entries[0]["path"] == "f.txt"
    assert entries[0]["additions"] == 1


# --- Structured output: log ---

@pytest.mark.asyncio
async def test_git_log_structured(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _run(repo, "add", "f.txt")
    _run(repo, "commit", "-m", "first")
    (repo / "f.txt").write_text("y\n", encoding="utf-8")
    _run(repo, "add", "f.txt")
    _run(repo, "commit", "-m", "second")

    result = await GitTool().execute(
        {"args": "log"},
        ToolContext(workspace=str(repo)),
    )
    entries = _payload(result)["data"]["entries"]
    assert len(entries) == 2
    assert entries[0]["message"] == "second"
    assert entries[1]["message"] == "first"


@pytest.mark.asyncio
async def test_git_log_with_limit(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    for i in range(5):
        (repo / "f.txt").write_text(f"v{i}\n", encoding="utf-8")
        _run(repo, "add", "f.txt")
        _run(repo, "commit", "-m", f"commit {i}")

    result = await GitTool().execute(
        {"args": "log -n 2"},
        ToolContext(workspace=str(repo)),
    )
    entries = _payload(result)["data"]["entries"]
    assert len(entries) == 2


@pytest.mark.asyncio
async def test_git_log_with_author(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _run(repo, "add", "f.txt")
    _run(repo, "commit", "-m", "init")

    result = await GitTool().execute(
        {"args": "log --author=VoidX"},
        ToolContext(workspace=str(repo)),
    )
    entries = _payload(result)["data"]["entries"]
    assert len(entries) == 1



@pytest.mark.asyncio
async def test_git_log_invalid_n_value_warns(tmp_path):
    """E3: invalid -n value should be surfaced, not silently ignored."""
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _run(repo, "add", "f.txt")
    _run(repo, "commit", "-m", "init")

    result = await GitTool().execute(
        {"args": "log -n abc"},
        ToolContext(workspace=str(repo)),
    )
    payload = _payload(result)
    assert payload["ok"] is True
    note = payload["data"].get("limit_note") or result.metadata.get("limit_note")
    assert note and "abc" in note and "default" in note.lower()

# --- Structured output: blame ---

@pytest.mark.asyncio
async def test_git_blame_structured(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("line1\nline2\n", encoding="utf-8")
    _run(repo, "add", "f.txt")
    _run(repo, "commit", "-m", "init")

    result = await GitTool().execute(
        {"args": "blame f.txt"},
        ToolContext(workspace=str(repo)),
    )
    entries = _payload(result)["data"]["entries"]
    assert len(entries) == 2
    assert entries[0]["content"] == "line1"
    assert entries[1]["content"] == "line2"


@pytest.mark.asyncio
async def test_git_blame_with_range(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("l1\nl2\nl3\nl4\nl5\n", encoding="utf-8")
    _run(repo, "add", "f.txt")
    _run(repo, "commit", "-m", "init")

    result = await GitTool().execute(
        {"args": "blame -L 2,3 f.txt"},
        ToolContext(workspace=str(repo)),
    )
    entries = _payload(result)["data"]["entries"]
    assert len(entries) == 2
    assert entries[0]["content"] == "l2"


@pytest.mark.asyncio
async def test_git_blame_requires_path(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _run(repo, "add", "f.txt")
    _run(repo, "commit", "-m", "init")

    result = await GitTool().execute(
        {"args": "blame"},
        ToolContext(workspace=str(repo)),
    )
    payload = _payload(result)
    assert payload["ok"] is False
    assert "path" in payload["error"]


# --- Structured output: show ---

@pytest.mark.asyncio
async def test_git_show_structured(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _run(repo, "add", "f.txt")
    _run(repo, "commit", "-m", "init")

    result = await GitTool().execute(
        {"args": "show HEAD"},
        ToolContext(workspace=str(repo)),
    )
    payload = _payload(result)
    assert payload["data"]["message"] == "init"
    assert "f.txt" in payload["data"]["files_changed"]


@pytest.mark.asyncio
async def test_git_show_stat_mode(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _run(repo, "add", "f.txt")
    _run(repo, "commit", "-m", "init")

    result = await GitTool().execute(
        {"args": "show --stat HEAD"},
        ToolContext(workspace=str(repo)),
    )
    payload = _payload(result)
    assert "stats" in payload["data"]
    assert payload["data"]["stats"]["additions"] >= 1


@pytest.mark.asyncio
async def test_git_show_ref_not_found(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _run(repo, "add", "f.txt")
    _run(repo, "commit", "-m", "init")

    result = await GitTool().execute(
        {"args": "show nonexistent"},
        ToolContext(workspace=str(repo)),
    )
    payload = _payload(result)
    assert payload["ok"] is False
    assert "nonexistent" in payload["error"] or "ref_not_found" in payload["error"]


# --- Structured output: branch list ---

@pytest.mark.asyncio
async def test_git_branch_list_structured(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _run(repo, "add", "f.txt")
    _run(repo, "commit", "-m", "init")
    _run(repo, "branch", "feature")

    result = await GitTool().execute(
        {"args": "branch"},
        ToolContext(workspace=str(repo)),
    )
    entries = _payload(result)["data"]["entries"]
    names = {e["name"] for e in entries}
    assert "feature" in names
    current = [e for e in entries if e["current"]]
    assert len(current) == 1


@pytest.mark.asyncio
async def test_git_branch_list_all(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _run(repo, "add", "f.txt")
    _run(repo, "commit", "-m", "init")

    result = await GitTool().execute(
        {"args": "branch --all"},
        ToolContext(workspace=str(repo)),
    )
    entries = _payload(result)["data"]["entries"]
    assert len(entries) >= 1


# --- Structured output: remote list ---

@pytest.mark.asyncio
async def test_git_remote_list_structured(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _run(repo, "add", "f.txt")
    _run(repo, "commit", "-m", "init")
    _run(repo, "remote", "add", "origin", "https://example.com/repo.git")

    result = await GitTool().execute(
        {"args": "remote -v"},
        ToolContext(workspace=str(repo)),
    )
    entries = _payload(result)["data"]["entries"]
    assert entries[0]["name"] == "origin"
    assert "example.com" in entries[0]["url"]


# --- Structured output: tag list ---

@pytest.mark.asyncio
async def test_git_tag_list_structured(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _run(repo, "add", "f.txt")
    _run(repo, "commit", "-m", "init")
    _run(repo, "tag", "v1.0")

    result = await GitTool().execute(
        {"args": "tag"},
        ToolContext(workspace=str(repo)),
    )
    entries = _payload(result)["data"]["entries"]
    assert entries[0]["name"] == "v1.0"


@pytest.mark.asyncio
async def test_git_tag_list_with_pattern(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _run(repo, "add", "f.txt")
    _run(repo, "commit", "-m", "init")
    _run(repo, "tag", "v1.0")
    _run(repo, "tag", "v2.0")
    _run(repo, "tag", "beta")

    result = await GitTool().execute(
        {"args": "tag -l v*"},
        ToolContext(workspace=str(repo)),
    )
    entries = _payload(result)["data"]["entries"]
    names = {e["name"] for e in entries}
    assert "v1.0" in names
    assert "v2.0" in names
    assert "beta" not in names


# --- Structured output: stash list ---

@pytest.mark.asyncio
async def test_git_stash_list_structured(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _run(repo, "add", "f.txt")
    _run(repo, "commit", "-m", "init")
    (repo / "f.txt").write_text("y\n", encoding="utf-8")
    _run(repo, "stash", "push", "-m", "wip")

    result = await GitTool().execute(
        {"args": "stash list"},
        ToolContext(workspace=str(repo)),
    )
    entries = _payload(result)["data"]["entries"]
    assert len(entries) == 1
    assert "wip" in entries[0]["message"]


@pytest.mark.asyncio
async def test_git_stash_list_empty(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _run(repo, "add", "f.txt")
    _run(repo, "commit", "-m", "init")

    result = await GitTool().execute(
        {"args": "stash list"},
        ToolContext(workspace=str(repo)),
    )
    entries = _payload(result)["data"]["entries"]
    assert len(entries) == 0


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
    assert payload["error"] == "command_denied"


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
    assert payload["error"] == "command_denied"


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
    assert payload["error"] == "command_denied"


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
    assert payload["error"] == "command_denied"


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
    assert payload["error"] == "command_denied"



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
