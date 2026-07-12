"""Phase 6 PowerShell containment policy tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from voidx.permission.grants import AccessGrants
from voidx.permission.process_sandbox import ProcessSandboxBackend, ProcessSandboxCapability
from voidx.tools.base import ToolContext
from voidx.tools.powershell.tool import PowerShellTool


def _payload(result):
    return json.loads(result.output)


@pytest.mark.asyncio
async def test_powershell_external_read_requires_write(tmp_path: Path):
    from voidx.permission.context import PermissionContext
    from voidx.permission.shell_policy import shell_sandbox_precheck

    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    (external / "data.txt").write_text("secret", encoding="utf-8")

    action, reason = shell_sandbox_precheck(
        {"command": f"Get-Content '{external / 'data.txt'}'"},
        PermissionContext(
            workspace=str(workspace),
            sandbox_mode="workspace-write",
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
