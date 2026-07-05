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


