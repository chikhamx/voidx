"""Phase 6 bash tool process containment tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from voidx.tooling.domain.grants import AccessGrants
from voidx.tooling.domain.process_sandbox import (
    ProcessSandboxBackend,
    ProcessSandboxCapability,
)
from voidx.tooling.application.execution import AuthorizationRuntime, ShellToolContext as ToolContext
from voidx.tooling.builtin.shell.bash.tool import BashTool


def _payload(result):
    return {
        "ok": result.metadata.get("ok", not result.metadata.get("error", False)),
        "blocked": result.metadata.get("blocked", False),
        "exit_code": result.metadata.get("exit_code"),
        "stdout": result.output,
        "stderr": result.metadata.get("stderr", result.output),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [False, True])
async def test_shell_tool_blocks_catastrophic_command_even_when_approved(tmp_path: Path, approved: bool):
    command = "rm -rf /"
    approved_risks = (
        [{
            "tool_name": "bash",
            "pattern": command,
            "risk_level": "blocked",
            "tags": ["system_destructive"],
            "reason": "test approval must not bypass hard block",
            "approved_by": "user",
        }]
        if approved
        else []
    )

    result = await BashTool().execute(
        {"command": command},
        ToolContext(workspace=str(tmp_path), approved_tool_risks=approved_risks),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["blocked"] is True
    assert "catastrophic" in payload["stderr"]


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
async def test_shell_external_read_uses_readable_grant(tmp_path: Path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    (external / "data.txt").write_text("secret", encoding="utf-8")

    result = await BashTool().execute(
        {"command": f"ls {external / 'data.txt'}"},
        ToolContext(
            workspace=str(workspace),
            authorization_service=AuthorizationRuntime(
                access_grants_reader=lambda: AccessGrants.from_parts(readable_dirs=[str(external)]),
            ),
            process_sandbox=ProcessSandboxCapability(backend=ProcessSandboxBackend.TEST, supported=True),
        ),
    )

    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["blocked"] is False
    assert "data.txt" in payload["stdout"]


@pytest.mark.asyncio
async def test_shell_external_read_without_grant_is_blocked(tmp_path: Path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    target = external / "data.txt"
    target.write_text("secret", encoding="utf-8")

    result = await BashTool().execute(
        {"command": f"ls {target}"},
        ToolContext(
            workspace=str(workspace),
            process_sandbox=ProcessSandboxCapability(backend=ProcessSandboxBackend.TEST, supported=True),
        ),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["blocked"] is True
    assert "access grant" in payload["stderr"]


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
            permission_mode="read_only",
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
            permission_mode="read_only",
            approved_tool_risks=[{"tool_name": "bash", "pattern": "printf other > out.txt", "risk_level": "dangerous"}],
        ),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["blocked"] is True
    assert not (tmp_path / "out.txt").exists()


@pytest.mark.asyncio
async def test_shell_tool_approved_risk_does_not_bypass_external_write_grant(tmp_path: Path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    target = external / "out.txt"
    command = f"printf approved > '{target}'"

    result = await BashTool().execute(
        {"command": command},
        ToolContext(
            workspace=str(workspace),
            permission_mode="read_only",
            approved_tool_risks=[{"tool_name": "bash", "pattern": command, "risk_level": "dangerous"}],
            process_sandbox=ProcessSandboxCapability(backend=ProcessSandboxBackend.TEST, supported=True),
        ),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["blocked"] is True
    assert not target.exists()


@pytest.mark.asyncio
async def test_shell_tool_hard_block_wins_over_full_access_and_approval(tmp_path: Path):
    command = "sudo true"

    result = await BashTool().execute(
        {"command": command},
        ToolContext(
            workspace=str(tmp_path),
            permission_mode="full_access",
            approved_tool_risks=[{"tool_name": "bash", "pattern": command, "risk_level": "dangerous"}],
        ),
    )

    assert result.metadata["blocked"] is True
    assert result.metadata["error"] is True
    assert result.metadata["exit_code"] == -1


@pytest.mark.asyncio
async def test_shell_tool_defer_does_not_start_subprocess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    called = False

    async def fail_if_started(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("deferred shell command must not start a subprocess")

    monkeypatch.setattr("voidx.tooling.builtin.shell.bash.tool.create_owned_subprocess_shell", fail_if_started)
    result = await BashTool().execute(
        {"command": "python -c 'print(1)'"},
        ToolContext(
            workspace=str(tmp_path),
            process_sandbox=ProcessSandboxCapability(backend=ProcessSandboxBackend.TEST, supported=True),
        ),
    )

    assert called is False
    assert result.metadata["blocked"] is True


@pytest.mark.asyncio
async def test_shell_tool_approved_risk_still_requires_external_read_grant(tmp_path: Path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    target = external / "data.txt"
    target.write_text("secret", encoding="utf-8")
    command = f"ls '{target}'"

    result = await BashTool().execute(
        {"command": command},
        ToolContext(
            workspace=str(workspace),
            approved_tool_risks=[{"tool_name": "bash", "pattern": command, "risk_level": "dangerous"}],
            process_sandbox=ProcessSandboxCapability(backend=ProcessSandboxBackend.TEST, supported=True),
        ),
    )

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["blocked"] is True
    assert "access grant" in payload["stderr"]


@pytest.mark.asyncio


@pytest.mark.asyncio
async def test_shell_tool_keeps_hard_block_with_approved_shell_risk_token(tmp_path: Path):
    command = "sudo true"

    result = await BashTool().execute(
        {"command": command},
        ToolContext(
            workspace=str(tmp_path),
            permission_mode="full_access",
            approved_tool_risks=[{"tool_name": "bash", "pattern": command, "risk_level": "blocked"}],
        ),
    )

    assert result.metadata["blocked"] is True
    assert result.metadata["error"] is True
