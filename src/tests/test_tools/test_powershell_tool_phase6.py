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
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    (external / "data.txt").write_text("secret", encoding="utf-8")

    result = await PowerShellTool().execute(
        {"command": f"Get-Content '{external / 'data.txt'}'"},
        ToolContext(
            workspace=str(workspace),
            get_access_grants=lambda: AccessGrants.from_parts(readable_dirs=[str(external)]),
            process_sandbox=ProcessSandboxCapability(backend=ProcessSandboxBackend.TEST, supported=True),
        ),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["blocked"] is True
    assert "writable grant" in payload["stderr"]


@pytest.mark.asyncio
async def test_powershell_requires_process_sandbox_backend(tmp_path: Path):
    result = await PowerShellTool().execute(
        {"command": "Get-Content README.md"},
        ToolContext(workspace=str(tmp_path)),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["blocked"] is True
    assert "process sandbox" in payload["stderr"]
