"""Phase 5 git tool runtime access plan and environment tests."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from voidx.permission.grants import AccessGrants
from voidx.tools.base import ToolContext
from voidx.tools.git import GitTool


def _run(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    return result.stdout


def _init_repo(path: Path) -> Path:
    path.mkdir()
    _run(path, "init")
    _run(path, "config", "user.email", "voidx@example.com")
    _run(path, "config", "user.name", "VoidX Tests")
    (path / "f.txt").write_text("x\n", encoding="utf-8")
    _run(path, "add", "f.txt")
    _run(path, "commit", "-m", "init")
    return path


def _payload(result):
    return json.loads(result.output)


@pytest.mark.asyncio
async def test_git_external_path_must_be_repo_root(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = _init_repo(tmp_path / "repo")
    child = repo / "nested"
    child.mkdir()

    result = await GitTool().execute(
        {"path": str(child), "args": "status"},
        ToolContext(
            workspace=str(workspace),
            get_access_grants=lambda: AccessGrants.from_parts(readable_dirs=[str(repo)]),
        ),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"] == "git_policy_denied: external path must be repository root"


@pytest.mark.asyncio
async def test_git_requires_linked_worktree_common_dir(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = _init_repo(tmp_path / "repo")
    linked = tmp_path / "linked"
    _run(repo, "worktree", "add", str(linked))

    result = await GitTool().execute(
        {"path": str(linked), "args": "status"},
        ToolContext(
            workspace=str(workspace),
            get_access_grants=lambda: AccessGrants.from_parts(readable_dirs=[str(linked)]),
        ),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"] == "git_policy_denied: runtime access plan requires authorization"


@pytest.mark.asyncio
async def test_git_requires_alternate_object_dirs(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = _init_repo(tmp_path / "repo")
    alternate = tmp_path / "objects-alt"
    alternate.mkdir()
    info = repo / ".git" / "objects" / "info"
    info.mkdir(exist_ok=True)
    (info / "alternates").write_text(str(alternate) + "\n", encoding="utf-8")

    result = await GitTool().execute(
        {"path": str(repo), "args": "status"},
        ToolContext(
            workspace=str(workspace),
            get_access_grants=lambda: AccessGrants.from_parts(readable_dirs=[str(repo)]),
        ),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"] == "git_policy_denied: runtime access plan requires authorization"


@pytest.mark.asyncio
async def test_git_sanitizes_path_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = _init_repo(tmp_path / "repo")
    fake_git_dir = tmp_path / "fake-git-dir"
    fake_git_dir.mkdir()
    monkeypatch.setenv("GIT_DIR", str(fake_git_dir))

    result = await GitTool().execute(
        {"path": str(repo), "args": "status"},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is True


@pytest.mark.asyncio
async def test_git_config_include_requires_grant(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = _init_repo(tmp_path / "repo")
    included = tmp_path / "included.gitconfig"
    included.write_text("[alias]\n    st = status\n", encoding="utf-8")
    _run(repo, "config", "include.path", str(included))

    result = await GitTool().execute(
        {"path": str(repo), "args": "status"},
        ToolContext(
            workspace=str(workspace),
            get_access_grants=lambda: AccessGrants.from_parts(readable_dirs=[str(repo)]),
        ),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"] == "git_policy_denied: runtime access plan requires authorization"


@pytest.mark.asyncio
async def test_git_rejects_unplanned_config_origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = _init_repo(tmp_path / "repo")
    global_config = tmp_path / "global.gitconfig"
    global_config.write_text("[alias]\n    st = status\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))

    result = await GitTool().execute(
        {"path": str(repo), "args": "status"},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"] == "git_policy_denied: unplanned config origin"


@pytest.mark.asyncio
async def test_git_denies_implicit_executable_config(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    _run(repo, "config", "core.fsmonitor", "/tmp/evil-fsmonitor")

    result = await GitTool().execute(
        {"path": str(repo), "args": "status"},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"] == "git_policy_denied: dangerous executable config"


@pytest.mark.asyncio
async def test_git_allows_local_credential_helper_for_read_only_status(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    _run(repo, "config", "credential.helper", "store --file=.git/credentials")

    result = await GitTool().execute(
        {"path": str(repo), "args": "status --short"},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is True


@pytest.mark.asyncio
async def test_git_unknown_raw_policy_denied(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")

    result = await GitTool().execute(
        {"path": str(repo), "args": "submodule update"},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"] == "git_policy_denied: command is not registered"


@pytest.mark.asyncio
async def test_git_denies_config_env_dangerous_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setenv("VOIDX_EVIL", "/tmp/evil-fsmonitor")

    result = await GitTool().execute(
        {"path": str(repo), "args": "--config-env=core.fsmonitor=VOIDX_EVIL status"},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"] == "git_policy_denied: dangerous global config"


@pytest.mark.asyncio
async def test_git_nested_config_include_requires_grant(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = _init_repo(tmp_path / "repo")
    first_include = tmp_path / "first.gitconfig"
    nested_include = tmp_path / "nested.gitconfig"
    first_include.write_text(f"[include]\n    path = {nested_include}\n", encoding="utf-8")
    nested_include.write_text("[alias]\n    st = status\n", encoding="utf-8")
    _run(repo, "config", "include.path", str(first_include))

    result = await GitTool().execute(
        {"path": str(repo), "args": "status"},
        ToolContext(
            workspace=str(workspace),
            get_access_grants=lambda: AccessGrants.from_parts(
                readable_dirs=[str(repo)],
                readable_files=[str(first_include)],
            ),
        ),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"] == "git_policy_denied: runtime access plan requires authorization"


@pytest.mark.asyncio
async def test_git_include_if_config_requires_grant(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = _init_repo(tmp_path / "repo")
    conditional_include = tmp_path / "conditional.gitconfig"
    conditional_include.write_text("[alias]\n    st = status\n", encoding="utf-8")
    with (repo / ".git" / "config").open("a", encoding="utf-8") as handle:
        handle.write(f"\n[includeIf \"gitdir:{repo}/\"]\n    path = {conditional_include}\n")

    result = await GitTool().execute(
        {"path": str(repo), "args": "status"},
        ToolContext(
            workspace=str(workspace),
            get_access_grants=lambda: AccessGrants.from_parts(readable_dirs=[str(repo)]),
        ),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"] == "git_policy_denied: runtime access plan requires authorization"


@pytest.mark.asyncio
async def test_git_tilde_config_include_requires_grant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = _init_repo(tmp_path / "repo")
    home = tmp_path / "home"
    home.mkdir()
    tilde_include = home / "included.gitconfig"
    tilde_include.write_text("[alias]\n    st = status\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    with (repo / ".git" / "config").open("a", encoding="utf-8") as handle:
        handle.write("\n[include]\n    path = ~/included.gitconfig\n")

    result = await GitTool().execute(
        {"path": str(repo), "args": "status"},
        ToolContext(
            workspace=str(workspace),
            get_access_grants=lambda: AccessGrants.from_parts(readable_dirs=[str(repo)]),
        ),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"] == "git_policy_denied: runtime access plan requires authorization"


@pytest.mark.asyncio
async def test_git_tilde_config_include_dangerous_config_denied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = _init_repo(tmp_path / "repo")
    home = tmp_path / "home"
    home.mkdir()
    tilde_include = home / "included.gitconfig"
    tilde_include.write_text("[core]\n    fsmonitor = /tmp/evil-fsmonitor\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    with (repo / ".git" / "config").open("a", encoding="utf-8") as handle:
        handle.write("\n[include]\n    path = ~/included.gitconfig\n")

    result = await GitTool().execute(
        {"path": str(repo), "args": "status"},
        ToolContext(
            workspace=str(repo),
            get_access_grants=lambda: AccessGrants.from_parts(
                readable_dirs=[str(repo)],
                readable_files=[str(tilde_include)],
            ),
        ),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"] == "git_policy_denied: dangerous executable config"


@pytest.mark.asyncio
async def test_git_diff_external_config_denied(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    _run(repo, "config", "diff.external", "/tmp/evil-diff")
    (repo / "f.txt").write_text("changed\n", encoding="utf-8")

    result = await GitTool().execute(
        {"path": str(repo), "args": "diff"},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"] == "git_policy_denied: dangerous executable config"


@pytest.mark.asyncio
async def test_git_config_worktree_dangerous_config_denied(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    with (repo / ".git" / "config").open("a", encoding="utf-8") as handle:
        handle.write("\n[extensions]\n    worktreeConfig = true\n")
    (repo / ".git" / "config.worktree").write_text(
        "[core]\n    fsmonitor = /tmp/evil-fsmonitor\n",
        encoding="utf-8",
    )

    result = await GitTool().execute(
        {"path": str(repo), "args": "status"},
        ToolContext(workspace=str(repo)),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"] == "git_policy_denied: dangerous executable config"


@pytest.mark.asyncio
async def test_git_config_worktree_include_requires_grant(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = _init_repo(tmp_path / "repo")
    external_config = tmp_path / "worktree-include.gitconfig"
    external_config.write_text("[alias]\n    st = status\n", encoding="utf-8")
    with (repo / ".git" / "config").open("a", encoding="utf-8") as handle:
        handle.write("\n[extensions]\n    worktreeConfig = true\n")
    (repo / ".git" / "config.worktree").write_text(
        f"[include]\n    path = {external_config}\n",
        encoding="utf-8",
    )

    result = await GitTool().execute(
        {"path": str(repo), "args": "status"},
        ToolContext(
            workspace=str(workspace),
            get_access_grants=lambda: AccessGrants.from_parts(readable_dirs=[str(repo)]),
        ),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"] == "git_policy_denied: runtime access plan requires authorization"


@pytest.mark.asyncio
async def test_git_unknown_leading_global_options_are_denied(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")

    for args in ("--bare status", "--paginate status", "--literal-pathspecs status", "-- status"):
        result = await GitTool().execute(
            {"path": str(repo), "args": args},
            ToolContext(workspace=str(repo)),
        )
        payload = _payload(result)
        assert payload["ok"] is False
        assert payload["error"] == "git_policy_denied: global option is not registered"


@pytest.mark.asyncio
async def test_git_external_child_path_denied_before_repo_discovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from voidx.tools.git import tool as git_tool_mod

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = _init_repo(tmp_path / "repo")
    child = repo / "nested"
    child.mkdir()
    called = False

    async def fail_discover(_ctx):
        nonlocal called
        called = True
        raise AssertionError("repo discovery should not run for external non-root path")

    monkeypatch.setattr(git_tool_mod, "_discover_repo", fail_discover)

    result = await GitTool().execute(
        {"path": str(child), "args": "status"},
        ToolContext(
            workspace=str(workspace),
            get_access_grants=lambda: AccessGrants.from_parts(readable_dirs=[str(repo)]),
        ),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"] == "git_policy_denied: external path must be repository root"
    assert called is False


@pytest.mark.asyncio
async def test_git_denies_editor_and_askpass_executable_config(tmp_path: Path):
    dangerous = {
        "core.editor": "/tmp/evil-editor",
        "sequence.editor": "/tmp/evil-sequence-editor",
        "core.askpass": "/tmp/evil-askpass",
        "gpg.program": "/tmp/evil-gpg",
        "gpg.ssh.program": "/tmp/evil-ssh-gpg",
    }
    for key, value in dangerous.items():
        repo = _init_repo(tmp_path / key.replace(".", "-"))
        _run(repo, "config", key, value)

        result = await GitTool().execute(
            {"path": str(repo), "args": "status"},
            ToolContext(workspace=str(repo)),
        )
        payload = _payload(result)
        assert payload["ok"] is False
        assert payload["error"] == "git_policy_denied: dangerous executable config"
