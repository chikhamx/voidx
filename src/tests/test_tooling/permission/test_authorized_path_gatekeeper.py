"""Pure gatekeeper behavior of authorized_path after permission normalization."""

from __future__ import annotations

from pathlib import Path

import pytest

from voidx.tooling.application.authorization import authorized_path
from voidx.tooling.application.execution import AuthorizationRuntime, FileToolContext as ToolContext
from voidx.tooling.domain.grants import AccessGrants


def _ctx(workspace: Path, grants: AccessGrants | None = None) -> ToolContext:
    return ToolContext(
        workspace=str(workspace),
        authorization_service=AuthorizationRuntime(
            access_grants_reader=(lambda: grants) if grants is not None else None,
        ),
    )


@pytest.mark.asyncio
async def test_external_path_without_grant_blocked_without_interaction(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external.txt"
    external.write_text("x", encoding="utf-8")

    path, error = await authorized_path(_ctx(workspace), str(external), write=False, require_exists=True)

    assert path is None
    assert error is not None
    assert error.metadata.get("error") is True
    assert error.metadata.get("unauthorized") is True
    assert "Path traversal blocked" in error.output


@pytest.mark.asyncio
async def test_external_missing_file_with_grant_passes_gate(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    missing = tmp_path / "missing.txt"
    grants = AccessGrants.from_parts(readable_files=[str(missing)])

    path, error = await authorized_path(_ctx(workspace, grants), str(missing), write=False, require_exists=True)

    assert error is None
    assert path == missing.resolve()


@pytest.mark.asyncio
async def test_workspace_path_passes_without_grant(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inner = workspace / "inner.txt"
    inner.write_text("x", encoding="utf-8")

    path, error = await authorized_path(_ctx(workspace), str(inner), write=False)

    assert error is None
    assert path == inner.resolve()
