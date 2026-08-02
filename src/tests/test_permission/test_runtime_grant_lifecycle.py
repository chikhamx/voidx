"""Tests for runtime grant lifecycle: readable by get_access_grants, cleaned after execution."""

from __future__ import annotations

import pytest

from voidx.permission.grants import AccessGrant, resolve_access, AccessGrants
from voidx.permission.service import PermissionService


@pytest.mark.asyncio
async def test_runtime_grant_readable_by_get_access_grants():
    service = PermissionService()
    grant = AccessGrant(path="/external/file.txt", access="write", object_type="file", persistence="runtime")
    await service.add_grant(grant)

    access_grants = service.get_access_grants()
    assert "/external/file.txt" in access_grants.writable_files


@pytest.mark.asyncio
async def test_runtime_grant_resolved_as_allow_by_resolve_access():
    service = PermissionService()
    grant = AccessGrant(path="/external/file.txt", access="write", object_type="file", persistence="runtime")
    await service.add_grant(grant)

    access_grants = service.get_access_grants()
    resolution = resolve_access(
        "/workspace",
        "/external/file.txt",
        access="write",
        access_grants=access_grants,
        allow_missing_write_file=True,
    )
    assert resolution.action == "allow"


@pytest.mark.asyncio
async def test_runtime_grants_cleared_after_execution_lease(tmp_path):
    service = PermissionService()
    grant = AccessGrant(path="/external/file.txt", access="write", object_type="file", persistence="runtime")
    await service.add_grant(grant)
    assert len(service._runtime_grants) == 1

    async with service.execution_lease_for_tool("write"):
        assert len(service._runtime_grants) == 1

    assert len(service._runtime_grants) == 0


@pytest.mark.asyncio
async def test_session_grants_survive_execution_lease(tmp_path):
    service = PermissionService()
    grant = AccessGrant(path="/external/file.txt", access="write", object_type="file", persistence="session")
    await service.add_grant(grant)

    async with service.execution_lease_for_tool("write"):
        pass

    assert len(service._session_grants) == 1
