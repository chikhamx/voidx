"""Regression coverage for the shell authorization entry point."""

from pathlib import Path

import pytest

from voidx.tooling.application.authorization import authorize_tool_call
from voidx.tooling.domain.authorization import PermissionContext
from voidx.tooling.domain.grants import AccessGrants
from voidx.tooling.domain.process_sandbox import ProcessSandboxBackend, ProcessSandboxCapability
from voidx.tooling.policy.shell.policy import shell_sandbox_precheck


@pytest.mark.parametrize("shell,command", [("bash", "cat"), ("powershell", "Get-Content")])
@pytest.mark.parametrize("grant,expected", [("none", "defer"), ("read", "allow"), ("write", "allow")])
def test_external_read_grants(tmp_path: Path, shell: str, command: str, grant: str, expected: str):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external.txt"
    external.write_text("data")
    grants = AccessGrants.from_parts(
        readable_files=[str(external)] if grant == "read" else [],
        writable_files=[str(external)] if grant == "write" else [],
    )
    context = PermissionContext(
        workspace=str(workspace), access_grants=grants,
        process_sandbox=ProcessSandboxCapability(backend=ProcessSandboxBackend.TEST, supported=True),
    )
    assert shell_sandbox_precheck({"command": f"{command} {external}"}, context, shell=shell)[0] == expected


@pytest.mark.parametrize("grant,expected", [("none", "defer"), ("read", "defer"), ("write", "allow")])
def test_external_write_requires_write_grant(tmp_path: Path, monkeypatch, grant: str, expected: str):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "output.txt"
    from voidx.tooling.policy.shell import policy

    monkeypatch.setattr(
        policy, "shell_policy_for_command",
        lambda *args, **kwargs: policy.ShellPolicyDecision(
            allowed=True, read_only=False, access_paths=(external,), write_paths=(external,),
        ),
    )
    context = PermissionContext(
        workspace=str(workspace),
        access_grants=AccessGrants.from_parts(
            readable_files=[str(external)] if grant == "read" else [],
            writable_files=[str(external)] if grant == "write" else [],
        ),
        process_sandbox=ProcessSandboxCapability(backend=ProcessSandboxBackend.TEST, supported=True),
    )
    assert shell_sandbox_precheck({"command": f"touch {external}"}, context)[0] == expected


@pytest.mark.parametrize("supported", [False, True])
def test_deferred_policy_is_not_implicitly_allowed(tmp_path: Path, supported: bool):
    context = PermissionContext(
        workspace=str(tmp_path),
        process_sandbox=ProcessSandboxCapability(backend=ProcessSandboxBackend.TEST, supported=supported),
    )
    call = {"name": "bash", "args": {"command": "echo $(cat secret.txt)"}}
    assert shell_sandbox_precheck(call["args"], context)[0] == "defer"
    assert authorize_tool_call(call, context).action == "ask"


def test_blocked_policy_stays_denied(tmp_path: Path):
    assert shell_sandbox_precheck(
        {"command": "rm -rf /"}, PermissionContext(workspace=str(tmp_path)),
    )[0] == "deny"


def test_full_sandbox_access_preserves_bypass(tmp_path: Path):
    assert shell_sandbox_precheck(
        {"command": "echo $(cat secret.txt)"},
        PermissionContext(workspace=str(tmp_path), permission_mode="full_access"),
    ) == ("allow", None)
