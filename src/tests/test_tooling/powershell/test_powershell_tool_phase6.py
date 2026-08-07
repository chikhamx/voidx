"""Phase 6 PowerShell containment policy tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from voidx.tooling.domain.grants import AccessGrants
from voidx.tooling.domain.process_sandbox import (
    ProcessSandboxBackend,
    ProcessSandboxCapability,
)
from voidx.tooling.application.execution import ShellToolContext as ToolContext
from voidx.tooling.builtin.shell.powershell.tool import PowerShellTool


def _payload(result):
    return json.loads(result.output)


@pytest.mark.asyncio
async def test_powershell_external_read_requires_write(tmp_path: Path):
    from voidx.tooling.domain.authorization import PermissionContext
    from voidx.tooling.policy.shell.policy import shell_sandbox_precheck

    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    (external / "data.txt").write_text("secret", encoding="utf-8")

    action, reason = shell_sandbox_precheck(
        {"command": f"Get-Content '{external / 'data.txt'}'"},
        PermissionContext(
            workspace=str(workspace),
            permission_mode="safe",
            access_grants=AccessGrants.from_parts(readable_dirs=[str(external)]),
            process_sandbox=ProcessSandboxCapability(backend=ProcessSandboxBackend.TEST, supported=True),
        ),
        shell="powershell",
    )

    assert action == "defer"
    assert "writable grant" in reason


@pytest.mark.asyncio
async def test_powershell_without_process_sandbox_fails_open(tmp_path: Path):
    result = await PowerShellTool().execute(
        {"command": "Write-Output hello"},
        ToolContext(workspace=str(tmp_path)),
    )

    assert result.metadata.get("error") is True


@pytest.mark.asyncio
async def test_powershell_tool_honors_exact_approved_shell_risk_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    async def fake_exec(*_args, **_kwargs):
        class Proc:
            returncode = 0

            async def communicate(self):
                return b"approved\n", b""

        return Proc()

    monkeypatch.setattr("voidx.tooling.builtin.shell.powershell.tool.create_owned_subprocess_exec", fake_exec)
    async def fake_release(_proc):
        return None

    monkeypatch.setattr("voidx.tooling.builtin.shell.powershell.tool.release_owned_process", fake_release)

    command = "Set-Content out.txt approved"
    result = await PowerShellTool().execute(
        {"command": command},
        ToolContext(
            workspace=str(tmp_path),
            permission_mode="read_only",
            approved_tool_risks=[{"tool_name": "powershell", "pattern": command, "risk_level": "dangerous"}],
        ),
    )

    payload = _payload(result)
    assert payload["ok"] is True
    assert "approved" in payload["stdout"]


@pytest.mark.asyncio
async def test_powershell_tool_rejects_non_matching_approved_shell_risk_token(tmp_path: Path):
    command = "Set-Content out.txt approved"
    result = await PowerShellTool().execute(
        {"command": command},
        ToolContext(
            workspace=str(tmp_path),
            permission_mode="read_only",
            approved_tool_risks=[{"tool_name": "powershell", "pattern": "Write-Output approved", "risk_level": "dangerous"}],
        ),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["blocked"] is True
