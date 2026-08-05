"""Phase 5 git limited policy and engine gate tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from voidx.permission.engine import PermissionContext, authorize_tool_call
from voidx.permission.grants import AccessGrants
from voidx.permission.risk import RiskTag
from voidx.permission.rules import build_pattern, capability_for_tool, classify_tool_call


def test_git_registered_for_each_ref_policy_allowed_in_workspace_write(tmp_path: Path):
    decision = authorize_tool_call(
        {"name": "git", "args": {"args": "for-each-ref --format=%(refname)"}},
        PermissionContext(workspace=str(tmp_path)),
    )

    assert decision.action == "allow"
    assert decision.source == "preset"


def test_git_registered_status_policy_remains_read_allowed(tmp_path: Path):
    decision = authorize_tool_call(
        {"name": "git", "args": {"args": "status --short"}},
        PermissionContext(workspace=str(tmp_path)),
    )

    assert decision.action == "allow"
    assert build_pattern("git", {"args": "status --short"}) == "read"


def test_git_read_policy_accepts_command_alias_and_display_name(tmp_path: Path):
    log_call = {
        "name": "Git",
        "args": {"command": "log --oneline -10 -- src/voidx/permission/git_policy.py"},
    }
    status_call = {
        "name": "git",
        "args": {"command": "status --porcelain -- src/voidx/permission/git_policy.py"},
    }

    log_decision = authorize_tool_call(
        log_call,
        PermissionContext(workspace=str(tmp_path)),
    )
    status_decision = authorize_tool_call(
        status_call,
        PermissionContext(workspace=str(tmp_path)),
    )
    classified = classify_tool_call(log_call)

    assert log_decision.action == "allow"
    assert status_decision.action == "allow"
    assert classified.name == "git"
    assert classified.capability.value == "git_read"
    assert classified.pattern == "read"


def test_git_dangerous_global_config_policy_denied(tmp_path: Path):
    decision = authorize_tool_call(
        {"name": "git", "args": {"args": "-c core.sshCommand=/tmp/evil status"}},
        PermissionContext(workspace=str(tmp_path)),
    )

    assert decision.action == "deny"
    assert "git policy" in decision.reason


def test_git_external_path_requires_grant_for_engine(tmp_path: Path):
    workspace = tmp_path / "workspace"
    external_repo = tmp_path / "external-repo"
    workspace.mkdir()
    external_repo.mkdir()

    decision = authorize_tool_call(
        {"name": "git", "args": {"path": str(external_repo), "args": "status"}},
        PermissionContext(workspace=str(workspace)),
    )

    assert decision.action == "ask"
    assert "outside workspace" in decision.reason


@pytest.mark.parametrize("permission_mode", ["ai_approval", "project_trusted"])
def test_git_external_path_requires_trusted_mode_approval(tmp_path: Path, permission_mode: str):
    workspace = tmp_path / "workspace"
    external_repo = tmp_path / "external-repo"
    workspace.mkdir()
    external_repo.mkdir()

    decision = authorize_tool_call(
        {"name": "git", "args": {"path": str(external_repo), "args": "status"}},
        PermissionContext(workspace=str(workspace), permission_mode=permission_mode),
    )

    assert decision.action == "ask"
    assert decision.access_intents
    assert decision.access_intents[0].access == "read"
    assert decision.risk is not None
    assert RiskTag.EXTERNAL_PATH in decision.risk.tags


@pytest.mark.parametrize("permission_mode", ["ai_approval", "project_trusted"])
@pytest.mark.parametrize("args", ["fetch origin", "pull --ff-only"])
def test_git_network_commands_require_trusted_mode_approval(tmp_path: Path, permission_mode: str, args: str):
    decision = authorize_tool_call(
        {"name": "git", "args": {"args": args}},
        PermissionContext(workspace=str(tmp_path), permission_mode=permission_mode),
    )

    assert decision.action == "ask"
    assert decision.risk is not None
    assert RiskTag.NETWORK in decision.risk.tags


def test_git_external_path_with_grant_allowed_by_engine(tmp_path: Path):
    workspace = tmp_path / "workspace"
    external_repo = tmp_path / "external-repo"
    workspace.mkdir()
    external_repo.mkdir()

    decision = authorize_tool_call(
        {"name": "git", "args": {"path": str(external_repo), "args": "status"}},
        PermissionContext(
            workspace=str(workspace),
            access_grants=AccessGrants.from_parts(readable_dirs=[str(external_repo)]),
        ),
    )

    assert decision.action == "allow"


def test_git_registered_for_each_ref_policy_is_classified_as_git_read():
    assert capability_for_tool("git", {"args": "for-each-ref --format=%(refname)"}).value == "git_read"


def test_git_config_env_global_option_denied(tmp_path: Path):
    decision = authorize_tool_call(
        {"name": "git", "args": {"args": "--config-env=core.fsmonitor=VOIDX_EVIL status"}},
        PermissionContext(workspace=str(tmp_path)),
    )

    assert decision.action == "deny"
    assert "git policy" in decision.reason
