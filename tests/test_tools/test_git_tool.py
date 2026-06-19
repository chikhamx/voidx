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


def _default_branch(repo: Path) -> str:
    return _run(repo, "symbolic-ref", "--short", "HEAD").strip()


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


@pytest.mark.asyncio
async def test_git_status_returns_branch(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")

    result = await GitTool().execute(
        {"command": "status"},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["data"]["branch"] in ("main", "master")


@pytest.mark.asyncio
async def test_git_show_structured(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")

    result = await GitTool().execute(
        {"command": "show"},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["data"]["hash"]
    assert payload["data"]["author"] == "VoidX Tests"
    assert payload["data"]["message"] == "initial"
    assert payload["data"]["merge"] is False
    assert "app.py" in payload["data"]["files_changed"]


@pytest.mark.asyncio
async def test_git_show_stat_mode(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")

    result = await GitTool().execute(
        {"command": "show", "args": {"stat": True}},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["data"]["hunks"] == []


@pytest.mark.asyncio
async def test_git_show_ref_not_found(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")

    result = await GitTool().execute(
        {"command": "show", "args": {"ref": "nonexistent"}},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"] == "ref_not_found"


@pytest.mark.asyncio
async def test_git_switch_creates_and_switches(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")

    result = await GitTool().execute(
        {"command": "switch", "args": {"branch": "feature-x", "create": True}},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["data"]["branch"] == "feature-x"
    assert payload["data"]["created"] is True
    assert payload["data"]["previous_branch"] in ("main", "master")


@pytest.mark.asyncio
async def test_git_switch_invalid_branch_name(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")

    result = await GitTool().execute(
        {"command": "switch", "args": {"branch": "../evil"}},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert "invalid branch name" in payload["error"]


@pytest.mark.asyncio
async def test_git_switch_branch_not_found(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")

    result = await GitTool().execute(
        {"command": "switch", "args": {"branch": "nonexistent"}},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"] == "branch_not_found"


@pytest.mark.asyncio
async def test_git_branch_create_and_delete(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")

    create_result = await GitTool().execute(
        {"command": "branch_create", "args": {"name": "feature-y"}},
        ToolContext(workspace=str(repo)),
    )
    create_payload = _payload(create_result)
    assert create_payload["ok"] is True
    assert create_payload["data"]["name"] == "feature-y"
    assert create_payload["data"]["hash"]

    delete_result = await GitTool().execute(
        {"command": "branch_delete", "args": {"name": "feature-y"}},
        ToolContext(workspace=str(repo)),
    )
    delete_payload = _payload(delete_result)
    assert delete_payload["ok"] is True
    assert delete_payload["data"]["name"] == "feature-y"


@pytest.mark.asyncio
async def test_git_branch_delete_current_branch_fails(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")
    branch_name = _run(repo, "symbolic-ref", "--short", "HEAD").strip()

    result = await GitTool().execute(
        {"command": "branch_delete", "args": {"name": branch_name}},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"] == "cannot_delete_current_branch"


@pytest.mark.asyncio
async def test_git_tag_create_list_delete(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")

    create_result = await GitTool().execute(
        {"command": "tag_create", "args": {"name": "v1.0.0", "message": "release"}},
        ToolContext(workspace=str(repo)),
    )
    create_payload = _payload(create_result)
    assert create_payload["ok"] is True
    assert create_payload["data"]["name"] == "v1.0.0"
    assert create_payload["data"]["annotated"] is True

    list_result = await GitTool().execute(
        {"command": "tag_list"},
        ToolContext(workspace=str(repo)),
    )
    list_payload = _payload(list_result)
    assert list_payload["ok"] is True
    assert any(e["name"] == "v1.0.0" for e in list_payload["data"]["entries"])

    delete_result = await GitTool().execute(
        {"command": "tag_delete", "args": {"name": "v1.0.0"}},
        ToolContext(workspace=str(repo)),
    )
    delete_payload = _payload(delete_result)
    assert delete_payload["ok"] is True
    assert delete_payload["data"]["name"] == "v1.0.0"


@pytest.mark.asyncio
async def test_git_stash_push_and_pop(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")
    (repo / "app.py").write_text("print('modified')\n", encoding="utf-8")

    push_result = await GitTool().execute(
        {"command": "stash_push", "args": {"message": "wip"}},
        ToolContext(workspace=str(repo)),
    )
    push_payload = _payload(push_result)
    assert push_payload["ok"] is True
    assert "wip" in push_payload["data"]["message"] or push_payload["data"]["message"]

    pop_result = await GitTool().execute(
        {"command": "stash_pop"},
        ToolContext(workspace=str(repo)),
    )
    pop_payload = _payload(pop_result)
    assert pop_payload["ok"] is True
    assert pop_payload["data"]["applied"] is True


@pytest.mark.asyncio
async def test_git_diff_with_base_ref(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('old')\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")
    _run(repo, "checkout", "-b", "feature")
    (repo / "app.py").write_text("print('new')\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "update")

    result = await GitTool().execute(
        {"command": "diff", "args": {"base": "HEAD~1", "ref": "feature"}},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is True
    assert any(e["path"] == "app.py" for e in payload["data"]["entries"])


@pytest.mark.asyncio
async def test_git_log_with_until(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")

    result = await GitTool().execute(
        {"command": "log", "args": {"limit": 5, "until": "2099-01-01"}},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is True
    assert len(payload["data"]["entries"]) >= 1


@pytest.mark.asyncio
async def test_git_commit_returns_hook_output(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _run(repo, "add", "app.py")

    result = await GitTool().execute(
        {"command": "commit", "args": {"message": "initial"}},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is True
    assert "hook_output" in payload["data"]


@pytest.mark.asyncio
async def test_git_switch_dirty_conflict(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("line1\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")
    main_branch = _default_branch(repo)
    _run(repo, "checkout", "-b", "feature")
    (repo / "app.py").write_text("feature-line1\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "feature change")
    _run(repo, "checkout", main_branch)
    (repo / "app.py").write_text("main-dirty-line1\n", encoding="utf-8")
    _run(repo, "add", "app.py")

    result = await GitTool().execute(
        {"command": "switch", "args": {"branch": "feature"}},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"] == "dirty_conflict"
    assert "dirty_files" in payload["data"]
    assert "stash_push" in payload["data"]["suggestion"]


@pytest.mark.asyncio
async def test_git_switch_dirty_no_conflict(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (repo / "other.txt").write_text("hello\n", encoding="utf-8")
    _run(repo, "add", "app.py", "other.txt")
    _run(repo, "commit", "-m", "initial")
    main_branch = _default_branch(repo)
    _run(repo, "checkout", "-b", "feature")
    (repo / "feature_file.txt").write_text("new\n", encoding="utf-8")
    _run(repo, "add", "feature_file.txt")
    _run(repo, "commit", "-m", "feature addition")
    _run(repo, "checkout", main_branch)
    (repo / "untracked_new.txt").write_text("dirty\n", encoding="utf-8")

    result = await GitTool().execute(
        {"command": "switch", "args": {"branch": "feature"}},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["data"]["branch"] == "feature"


@pytest.mark.asyncio
async def test_git_switch_with_start_point(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")
    commit_hash = _run(repo, "rev-parse", "HEAD").strip()

    result = await GitTool().execute(
        {"command": "switch", "args": {"branch": "from-commit", "create": True, "start_point": commit_hash}},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["data"]["created"] is True


@pytest.mark.asyncio
async def test_git_branch_create_with_start_point(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")

    result = await GitTool().execute(
        {"command": "branch_create", "args": {"name": "from-head", "start_point": "HEAD"}},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["data"]["name"] == "from-head"
    assert payload["data"]["start_point"] == "HEAD"


@pytest.mark.asyncio
async def test_git_branch_delete_not_merged(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")
    main_branch = _default_branch(repo)
    _run(repo, "checkout", "-b", "feature")
    (repo / "new_file.txt").write_text("new\n", encoding="utf-8")
    _run(repo, "add", "new_file.txt")
    _run(repo, "commit", "-m", "feature work")
    _run(repo, "checkout", main_branch)

    result = await GitTool().execute(
        {"command": "branch_delete", "args": {"name": "feature"}},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"] == "branch_not_merged"


@pytest.mark.asyncio
async def test_git_branch_delete_force_unmerged(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")
    main_branch = _default_branch(repo)
    _run(repo, "checkout", "-b", "feature")
    (repo / "new_file.txt").write_text("new\n", encoding="utf-8")
    _run(repo, "add", "new_file.txt")
    _run(repo, "commit", "-m", "feature work")
    _run(repo, "checkout", main_branch)

    result = await GitTool().execute(
        {"command": "branch_delete", "args": {"name": "feature", "force": True}},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["data"]["force"] is True


@pytest.mark.asyncio
async def test_git_branch_create_invalid_name(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")

    result = await GitTool().execute(
        {"command": "branch_create", "args": {"name": "evil..name"}},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert "invalid branch name" in payload["error"]


@pytest.mark.asyncio
async def test_git_tag_create_already_exists(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")

    await GitTool().execute(
        {"command": "tag_create", "args": {"name": "v1.0.0"}},
        ToolContext(workspace=str(repo)),
    )

    result = await GitTool().execute(
        {"command": "tag_create", "args": {"name": "v1.0.0"}},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"] == "tag_already_exists"


@pytest.mark.asyncio
async def test_git_tag_create_force_overwrite(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")

    await GitTool().execute(
        {"command": "tag_create", "args": {"name": "v1.0.0"}},
        ToolContext(workspace=str(repo)),
    )

    result = await GitTool().execute(
        {"command": "tag_create", "args": {"name": "v1.0.0", "force": True}},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is True


@pytest.mark.asyncio
async def test_git_tag_list_with_pattern(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")
    _run(repo, "tag", "v1.0.0")
    _run(repo, "tag", "v2.0.0")
    _run(repo, "tag", "release-candidate")

    result = await GitTool().execute(
        {"command": "tag_list", "args": {"pattern": "v*"}},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is True
    names = [e["name"] for e in payload["data"]["entries"]]
    assert "v1.0.0" in names
    assert "v2.0.0" in names
    assert "release-candidate" not in names


@pytest.mark.asyncio
async def test_git_tag_list_returns_hash(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")
    _run(repo, "tag", "v1.0.0")

    result = await GitTool().execute(
        {"command": "tag_list"},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is True
    entry = payload["data"]["entries"][0]
    assert entry["name"] == "v1.0.0"
    assert entry["hash"]


@pytest.mark.asyncio
async def test_git_tag_create_lightweight(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")

    result = await GitTool().execute(
        {"command": "tag_create", "args": {"name": "v0.1.0"}},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["data"]["annotated"] is False


@pytest.mark.asyncio
async def test_git_stash_push_with_pathspec(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (repo / "other.txt").write_text("hello\n", encoding="utf-8")
    _run(repo, "add", "app.py", "other.txt")
    _run(repo, "commit", "-m", "initial")
    (repo / "app.py").write_text("print('modified')\n", encoding="utf-8")
    (repo / "other.txt").write_text("modified\n", encoding="utf-8")

    result = await GitTool().execute(
        {"command": "stash_push", "args": {"pathspec": ["app.py"]}},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is True
    assert "app.py" in payload["data"]["files_stashed"]


@pytest.mark.asyncio
async def test_git_stash_push_pathspec_rejects_outside_workspace(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")

    result = await GitTool().execute(
        {"command": "stash_push", "args": {"pathspec": [str(outside)]}},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert "outside allowed workspace" in payload["error"]


@pytest.mark.asyncio
async def test_git_stash_pop_with_keep(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")
    (repo / "app.py").write_text("print('modified')\n", encoding="utf-8")

    await GitTool().execute(
        {"command": "stash_push", "args": {"message": "wip"}},
        ToolContext(workspace=str(repo)),
    )

    result = await GitTool().execute(
        {"command": "stash_pop", "args": {"keep": True}},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["data"]["applied"] is True
    assert payload["data"]["kept"] is True

    stash_list = _run(repo, "stash", "list")
    assert stash_list.strip()


@pytest.mark.asyncio
async def test_git_stash_pop_returns_files_restored(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")
    (repo / "app.py").write_text("print('modified')\n", encoding="utf-8")

    await GitTool().execute(
        {"command": "stash_push"},
        ToolContext(workspace=str(repo)),
    )

    result = await GitTool().execute(
        {"command": "stash_pop"},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["data"]["applied"] is True
    assert "app.py" in payload["data"]["files_restored"]


@pytest.mark.asyncio
async def test_git_show_with_pathspec(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (repo / "other.txt").write_text("hello\n", encoding="utf-8")
    _run(repo, "add", "app.py", "other.txt")
    _run(repo, "commit", "-m", "initial")

    result = await GitTool().execute(
        {"command": "show", "args": {"pathspec": ["app.py"]}},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is True
    assert "app.py" in payload["data"]["files_changed"]
    assert "other.txt" not in payload["data"]["files_changed"]


@pytest.mark.asyncio
async def test_git_show_stat_returns_files_and_stats(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")

    result = await GitTool().execute(
        {"command": "show", "args": {"stat": True}},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["data"]["files_changed"]
    assert "additions" in payload["data"]["stats"]
    assert "deletions" in payload["data"]["stats"]


@pytest.mark.asyncio
async def test_git_switch_denied_branch_names(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")

    for bad_name in ["evil..name", "name@{today}", "name~1", "name^", "name:foo", "name.lock"]:
        result = await GitTool().execute(
            {"command": "switch", "args": {"branch": bad_name, "create": True}},
            ToolContext(workspace=str(repo)),
        )
        payload = _payload(result)
        assert payload["ok"] is False, f"expected rejection for branch name: {bad_name}"
        assert "invalid branch name" in payload["error"]


@pytest.mark.asyncio
async def test_git_tag_delete_nonexistent(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")

    result = await GitTool().execute(
        {"command": "tag_delete", "args": {"name": "nonexistent"}},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is False


@pytest.mark.asyncio
async def test_git_stash_pop_nonexistent_index(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _run(repo, "add", "app.py")
    _run(repo, "commit", "-m", "initial")

    result = await GitTool().execute(
        {"command": "stash_pop", "args": {"index": 99}},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is False
