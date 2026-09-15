"""Bash execution against external paths after access grants (acceptance case 3 & 5)."""

from __future__ import annotations

import pytest

from voidx.tooling.application.execution import AuthorizationRuntime, ShellToolContext as ToolContext
from voidx.tooling.builtin.shell.bash.tool import BashTool
from voidx.tooling.domain.grants import AccessGrants

from .test_bash_git_direct import _init_repo


def _ctx(workspace, grants: AccessGrants) -> ToolContext:
    return ToolContext(
        workspace=str(workspace),
        authorization_service=AuthorizationRuntime(access_grants_reader=lambda: grants),
    )


@pytest.mark.asyncio
async def test_git_external_repo_executes_after_grant(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    grants = AccessGrants.from_parts(readable_dirs=[str(repo.resolve())])
    result = await BashTool().execute({"command": f"git -C {repo} log --oneline"}, _ctx(workspace, grants))

    assert result.metadata.get("exit_code") == 0
    assert "init" in result.output


@pytest.mark.asyncio
async def test_git_external_repo_blocked_without_grant(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    result = await BashTool().execute(
        {"command": f"git -C {repo} log --oneline"},
        _ctx(workspace, AccessGrants.from_parts()),
    )

    assert result.metadata.get("blocked") is True
    assert "external path requires" in result.output
