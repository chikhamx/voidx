from datetime import datetime
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


@pytest.mark.asyncio
async def test_git_optional_locks_only_for_read_processes(tmp_path, monkeypatch):
    captured_envs = []

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_exec(*args, cwd=None, env=None, stdin=None, stdout=None, stderr=None):
        captured_envs.append(env or {})
        return FakeProcess()

    monkeypatch.setattr(git_mod.asyncio, "create_subprocess_exec", fake_exec)

    await git_mod._run_process(["git", "status"], cwd=str(tmp_path), read_only=True)
    await git_mod._run_process(["git", "add", "file.txt"], cwd=str(tmp_path))

    assert captured_envs[0]["GIT_OPTIONAL_LOCKS"] == "0"
    assert "GIT_OPTIONAL_LOCKS" not in captured_envs[1]


@pytest.mark.asyncio
async def test_git_non_repo_returns_structured_error(tmp_path):
    result = await GitTool().execute(
        {"command": "status"},
        ToolContext(workspace=str(tmp_path)),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"] == "not_a_git_repository"


@pytest.mark.asyncio
async def test_git_status_structured(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("old\n", encoding="utf-8")
    _run(repo, "add", "tracked.txt")
    _run(repo, "commit", "-m", "initial")
    (repo / "tracked.txt").write_text("new\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("hello\n", encoding="utf-8")

    result = await GitTool().execute(
        {"command": "status"},
        ToolContext(workspace=str(repo)),
    )

    entries = _payload(result)["data"]["entries"]
    by_path = {entry["path"]: entry for entry in entries}
    assert by_path["tracked.txt"]["unstaged"] == "modified"
    assert by_path["untracked.txt"]["untracked"] is True


@pytest.mark.asyncio
async def test_git_diff_structured(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('old')\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")
    (repo / "app.py").write_text("print('new')\n", encoding="utf-8")

    result = await GitTool().execute(
        {"command": "diff", "args": {"pathspec": ["app.py"]}},
        ToolContext(workspace=str(repo)),
    )

    entries = _payload(result)["data"]["entries"]
    assert entries[0]["path"] == "app.py"
    assert entries[0]["additions"] == 1
    assert entries[0]["deletions"] == 1
    assert any("print('new')" in hunk for hunk in entries[0]["hunks"])


@pytest.mark.asyncio
async def test_git_diff_fetches_hunks_once_for_multiple_files(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    (repo / "a.txt").write_text("a1\n", encoding="utf-8")
    (repo / "b.txt").write_text("b1\n", encoding="utf-8")
    _run(repo, "add", "a.txt", "b.txt")
    _run(repo, "commit", "-m", "initial")
    (repo / "a.txt").write_text("a2\n", encoding="utf-8")
    (repo / "b.txt").write_text("b2\n", encoding="utf-8")

    calls = []
    original_run_git = git_mod._run_git

    async def capture_run_git(repo_model, args, **kwargs):
        calls.append(args)
        return await original_run_git(repo_model, args, **kwargs)

    monkeypatch.setattr(git_mod, "_run_git", capture_run_git)

    result = await GitTool().execute(
        {"command": "diff"},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is True
    assert {entry["path"] for entry in payload["data"]["entries"]} == {"a.txt", "b.txt"}
    unified_calls = [args for args in calls if args and args[0] == "diff" and "--unified=3" in args]
    assert len(unified_calls) == 1


@pytest.mark.asyncio
async def test_git_log_structured(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")

    result = await GitTool().execute(
        {"command": "log", "args": {"limit": 1, "path": "app.py"}},
        ToolContext(workspace=str(repo)),
    )

    entries = _payload(result)["data"]["entries"]
    assert len(entries) == 1
    assert entries[0]["message"] == "initial"
    assert entries[0]["files_changed"] == ["app.py"]


@pytest.mark.asyncio
async def test_git_blame_structured(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("one\ntwo\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")

    result = await GitTool().execute(
        {"command": "blame", "args": {"path": "app.py", "start": 2, "end": 2}},
        ToolContext(workspace=str(repo)),
    )

    entries = _payload(result)["data"]["entries"]
    assert entries == [
        {
            "line": 2,
            "commit": entries[0]["commit"],
            "author": "VoidX Tests",
            "date": entries[0]["date"],
            "content": "two",
        }
    ]
    datetime.fromisoformat(entries[0]["date"])
    assert not entries[0]["date"].isdigit()


@pytest.mark.asyncio
async def test_git_branch_and_remote_list_structured(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")
    _run(repo, "remote", "add", "origin", "https://example.com/repo.git")

    branch = await GitTool().execute(
        {"command": "branch_list"},
        ToolContext(workspace=str(repo)),
    )
    remote = await GitTool().execute(
        {"command": "remote_list"},
        ToolContext(workspace=str(repo)),
    )

    branches = _payload(branch)["data"]["entries"]
    remotes = _payload(remote)["data"]["entries"]
    assert any(entry["current"] for entry in branches)
    assert {"name": "origin", "url": "https://example.com/repo.git", "type": "fetch"} in remotes
    assert {"name": "origin", "url": "https://example.com/repo.git", "type": "push"} in remotes


@pytest.mark.asyncio
async def test_git_add_requires_paths(tmp_path):
    repo = _init_repo(tmp_path / "repo")

    result = await GitTool().execute(
        {"command": "add", "args": {"paths": []}},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert "paths" in payload["error"]


@pytest.mark.asyncio
async def test_git_commit_paths_does_not_stage_unrelated_dirty_files(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "a.txt").write_text("a1\n", encoding="utf-8")
    (repo / "b.txt").write_text("b1\n", encoding="utf-8")
    _run(repo, "add", "a.txt", "b.txt")
    _run(repo, "commit", "-m", "initial")
    (repo / "a.txt").write_text("a2\n", encoding="utf-8")
    (repo / "b.txt").write_text("b2\n", encoding="utf-8")

    result = await GitTool().execute(
        {"command": "commit", "args": {"message": "update a", "paths": ["a.txt"]}},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["data"]["files_changed"] == ["a.txt"]
    assert payload["data"]["unstaged_uncommitted"] == ["b.txt"]
    assert " M b.txt" in _run(repo, "status", "--short")


@pytest.mark.asyncio
async def test_git_commit_paths_skips_staged_files_query(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    (repo / "a.txt").write_text("a1\n", encoding="utf-8")
    _run(repo, "add", "a.txt")
    _run(repo, "commit", "-m", "initial")
    (repo / "a.txt").write_text("a2\n", encoding="utf-8")

    calls = []
    original_run_git = git_mod._run_git

    async def capture_run_git(repo_model, args, **kwargs):
        calls.append(args)
        return await original_run_git(repo_model, args, **kwargs)

    monkeypatch.setattr(git_mod, "_run_git", capture_run_git)

    result = await GitTool().execute(
        {"command": "commit", "args": {"message": "update a", "paths": ["a.txt"]}},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is True
    assert ["diff", "--cached", "--name-only"] not in calls


@pytest.mark.asyncio
async def test_git_commit_paths_does_not_commit_unrelated_staged_files(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "a.txt").write_text("a1\n", encoding="utf-8")
    (repo / "b.txt").write_text("b1\n", encoding="utf-8")
    _run(repo, "add", "a.txt", "b.txt")
    _run(repo, "commit", "-m", "initial")
    (repo / "a.txt").write_text("a2\n", encoding="utf-8")
    (repo / "b.txt").write_text("b2\n", encoding="utf-8")
    _run(repo, "add", "b.txt")

    result = await GitTool().execute(
        {"command": "commit", "args": {"message": "update a", "paths": ["a.txt"]}},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["data"]["files_changed"] == ["a.txt"]
    assert _run(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines() == ["a.txt"]
    assert _run(repo, "status", "--short").splitlines() == ["M  b.txt"]


@pytest.mark.asyncio
async def test_git_restore_is_path_scoped(tmp_path):
    repo = _init_repo(tmp_path / "repo")

    result = await GitTool().execute(
        {"command": "restore", "args": {"paths": []}},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert "paths" in payload["error"]


@pytest.mark.asyncio
async def test_git_restore_undoes_explicit_path(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    target = repo / "app.py"
    target.write_text("old\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")
    target.write_text("new\n", encoding="utf-8")

    result = await GitTool().execute(
        {"command": "restore", "args": {"paths": ["app.py"]}},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["data"]["restored"] == ["app.py"]
    assert target.read_text(encoding="utf-8") == "old\n"


@pytest.mark.asyncio
async def test_git_restore_rejects_outside_workspace(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")

    result = await GitTool().execute(
        {"command": "restore", "args": {"paths": [str(outside)]}},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert result.metadata["error"] is True
    assert "outside allowed workspace" in payload["error"]
