"""Phase 6 bash tool process containment tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from voidx.permission.grants import AccessGrants
from voidx.permission.process_sandbox import ProcessSandboxBackend, ProcessSandboxCapability
from voidx.tools.base import ToolContext
from voidx.tools.bash.tool import BashTool


def _payload(result):
    return json.loads(result.output)


@pytest.mark.asyncio
async def test_shell_sandbox_contains_child_process(tmp_path: Path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()

    result = await BashTool().execute(
        {"command": f"python -c 'open(\"{outside / 'escape.txt'}\", \"w\").write(\"x\")'"},
        ToolContext(
            workspace=str(workspace),
            process_sandbox=ProcessSandboxCapability(backend=ProcessSandboxBackend.TEST, supported=True),
        ),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["blocked"] is True
    assert "shell policy" in payload["stderr"] or "process sandbox" in payload["stderr"]
    assert not (outside / "escape.txt").exists()


@pytest.mark.asyncio
async def test_shell_allows_static_read_without_process_sandbox_backend(tmp_path: Path):
    (tmp_path / "allowed.txt").write_text("ok", encoding="utf-8")

    result = await BashTool().execute(
        {"command": "ls allowed.txt"},
        ToolContext(workspace=str(tmp_path)),
    )

    payload = _payload(result)
    assert payload["ok"] is True


@pytest.mark.asyncio
async def test_shell_external_read_requires_writable_grant(tmp_path: Path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    (external / "data.txt").write_text("secret", encoding="utf-8")

    result = await BashTool().execute(
        {"command": f"ls {external / 'data.txt'}"},
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
async def test_shell_tool_denies_glued_operator_before_execution(tmp_path: Path):
    (tmp_path / "README.md").write_text("ok", encoding="utf-8")

    result = await BashTool().execute(
        {"command": "cat README.md;touch escape.txt"},
        ToolContext(
            workspace=str(tmp_path),
            process_sandbox=ProcessSandboxCapability(backend=ProcessSandboxBackend.TEST, supported=True),
        ),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["blocked"] is True
    assert "shell policy" in payload["stderr"]
    assert not (tmp_path / "escape.txt").exists()


@pytest.mark.asyncio
async def test_shell_tool_denies_quote_boundary_operator_before_execution(tmp_path: Path):
    result = await BashTool().execute(
        {"command": "cat '\\';touch escape.txt"},
        ToolContext(
            workspace=str(tmp_path),
            process_sandbox=ProcessSandboxCapability(backend=ProcessSandboxBackend.TEST, supported=True),
        ),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["blocked"] is True
    assert "shell policy" in payload["stderr"]
    assert not (tmp_path / "escape.txt").exists()


@pytest.mark.asyncio
async def test_shell_tool_honors_exact_approved_shell_risk_token(tmp_path: Path):
    command = "printf approved > out.txt"

    result = await BashTool().execute(
        {"command": command},
        ToolContext(
            workspace=str(tmp_path),
            permission_preset="read_only",
            approved_tool_risks=[{"tool_name": "bash", "pattern": command, "risk_level": "dangerous"}],
        ),
    )

    payload = _payload(result)
    assert payload["ok"] is True
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "approved"


@pytest.mark.asyncio
async def test_shell_tool_rejects_non_matching_approved_shell_risk_token(tmp_path: Path):
    command = "printf approved > out.txt"

    result = await BashTool().execute(
        {"command": command},
        ToolContext(
            workspace=str(tmp_path),
            permission_preset="read_only",
            approved_tool_risks=[{"tool_name": "bash", "pattern": "printf other > out.txt", "risk_level": "dangerous"}],
        ),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["blocked"] is True
    assert not (tmp_path / "out.txt").exists()


@pytest.mark.asyncio
async def test_shell_tool_keeps_hard_block_with_approved_shell_risk_token(tmp_path: Path):
    command = "sudo true"

    result = await BashTool().execute(
        {"command": command},
        ToolContext(
            workspace=str(tmp_path),
            permission_preset="full_access",
            approved_tool_risks=[{"tool_name": "bash", "pattern": command, "risk_level": "blocked"}],
        ),
    )

    assert result.metadata["blocked"] is True
    assert result.metadata["error"] is True
