"""Tests verifying tool execution layer resolves access with runtime grants."""

from __future__ import annotations

from tests.tool_registry import build_registry
import pytest

from voidx.tooling.domain.grants import AccessGrant
from voidx.tooling.adapters.permission.in_memory_state import create_permission_service as PermissionService
from voidx.tooling.application.execution import (
    AuthorizationRuntime,
    CallbackInteractionPort,
    FileToolContext as ToolContext,
)
from voidx.tooling.domain.interaction import (
    UserInteraction,
    UserResponse,
)
from voidx.tooling.application.registry import ToolRegistry


@pytest.mark.asyncio
async def test_read_tool_skips_interact_when_runtime_grant_exists(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    target = external / "data.txt"
    target.write_text("content", encoding="utf-8")

    service = PermissionService()
    grant = AccessGrant(path=str(target), access="read", object_type="file", persistence="runtime")
    await service.add_grant(grant)

    interact_called = False

    async def interact(_req: UserInteraction) -> UserResponse:
        nonlocal interact_called
        interact_called = True
        return UserResponse(value="allow")

    ctx = ToolContext(
        workspace=str(workspace),
        authorization_service=AuthorizationRuntime(
            access_grants_reader=service.get_access_grants,
            grant_writer=service.add_grant,
            interaction=CallbackInteractionPort(interact),
        ),
    )

    result = await build_registry().execute_tool("read", {"file_path": str(target)}, ctx)

    assert result.metadata.get("error") is not True
    assert interact_called is False


@pytest.mark.asyncio
async def test_write_tool_skips_interact_when_runtime_grant_exists(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    target = external / "out.txt"

    service = PermissionService()
    grant = AccessGrant(path=str(target), access="write", object_type="file", persistence="runtime")
    await service.add_grant(grant)

    interact_called = False

    async def interact(_req: UserInteraction) -> UserResponse:
        nonlocal interact_called
        interact_called = True
        return UserResponse(value="allow")

    ctx = ToolContext(
        workspace=str(workspace),
        authorization_service=AuthorizationRuntime(
            access_grants_reader=service.get_access_grants,
            grant_writer=service.add_grant,
            interaction=CallbackInteractionPort(interact),
        ),
    )

    result = await build_registry().execute_tool(
        "write", {"file_path": str(target), "op": "write", "new_string": "data"},
        ctx,
    )

    assert result.metadata.get("error") is not True
    assert interact_called is False
