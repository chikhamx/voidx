"""Phase 2 permission grant state, revision, and lock tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from voidx.config import Settings
from voidx.permission.context import PermissionContext
from voidx.permission.engine import authorize_tool_call
from voidx.permission.grants import AccessGrant, AccessGrants, ApprovalPrecondition, GrantDelta, PathGrantLockManager, resolve_access
from voidx.permission.service import PermissionService
from voidx.tools.base import ToolContext, UserInteraction, UserResponse
from voidx.tools.registry import ToolRegistry


@pytest.mark.asyncio
async def test_context_grants_are_refreshed(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    target = external / "fresh.txt"
    target.write_text("fresh\n", encoding="utf-8")
    service = PermissionService()

    async def add_grant(grant: AccessGrant) -> None:
        await service.add_grant(grant)

    async def interact(_req: UserInteraction) -> UserResponse:
        return UserResponse(value="session_file")

    ctx = ToolContext(
        workspace=str(workspace),
        get_access_grants=service.get_access_grants,
        add_grant=add_grant,
        interact=interact,
    )

    first = await ToolRegistry().execute_tool("read", {"file_path": str(target)}, ctx)
    second = await ToolRegistry().execute_tool("read", {"file_path": str(target)}, ctx)

    assert first.metadata.get("error") is not True
    assert second.metadata.get("error") is not True
    assert service.state_revision == 1
    assert service.permissions_revision == 0


@pytest.mark.asyncio
async def test_persistent_tool_grant_commits_to_settings(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    target = external / "persistent.txt"
    target.write_text("persist\n", encoding="utf-8")
    settings = Settings(str(workspace))
    service = PermissionService(persistent_grant_writer=settings.add_persistent_grant_delta)

    async def interact(_req: UserInteraction) -> UserResponse:
        return UserResponse(value="persistent_file")

    ctx = ToolContext(
        workspace=str(workspace),
        get_access_grants=service.get_access_grants,
        add_grant=service.add_grant,
        acquire_grant_targets=service.acquire_grant_targets,
        interact=interact,
    )

    result = await ToolRegistry().execute_tool("read", {"file_path": str(target)}, ctx)

    assert result.metadata.get("error") is not True
    reloaded = Settings(str(workspace))
    assert reloaded.get_persistent_readable_files() == [str(target)]


@pytest.mark.asyncio
async def test_build_permission_service_persists_tool_grant_to_settings(tmp_path):
    from voidx.agent.graph.wiring import build_permission_service

    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    target = external / "wired.txt"
    target.write_text("wired\n", encoding="utf-8")
    settings = Settings(str(workspace))
    cfg = await (await Settings.create(str(workspace))).build_config()
    cfg.workspace = str(workspace)
    service = build_permission_service(cfg, settings=settings, notifier=lambda _msg: None)

    async def interact(_req: UserInteraction) -> UserResponse:
        return UserResponse(value="persistent_file")

    ctx = ToolContext(
        workspace=str(workspace),
        get_access_grants=service.get_access_grants,
        add_grant=service.add_grant,
        acquire_grant_targets=service.acquire_grant_targets,
        interact=interact,
    )

    result = await ToolRegistry().execute_tool("read", {"file_path": str(target)}, ctx)

    assert result.metadata.get("error") is not True
    assert Settings(str(workspace)).get_persistent_readable_files() == [str(target)]


@pytest.mark.asyncio
async def test_tool_grant_lock_serializes_prompts_for_same_path(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    target = external / "locked.txt"
    target.write_text("locked\n", encoding="utf-8")
    service = PermissionService()
    active_prompts = 0
    max_active_prompts = 0

    async def interact(_req: UserInteraction) -> UserResponse:
        nonlocal active_prompts, max_active_prompts
        active_prompts += 1
        max_active_prompts = max(max_active_prompts, active_prompts)
        await asyncio.sleep(0.05)
        active_prompts -= 1
        return UserResponse(value="session_file")

    def make_ctx() -> ToolContext:
        return ToolContext(
            workspace=str(workspace),
            get_access_grants=service.get_access_grants,
            add_grant=service.add_grant,
            acquire_grant_targets=service.acquire_grant_targets,
            interact=interact,
        )

    first, second = await asyncio.gather(
        ToolRegistry().execute_tool("read", {"file_path": str(target)}, make_ctx()),
        ToolRegistry().execute_tool("read", {"file_path": str(target)}, make_ctx()),
    )

    assert first.metadata.get("error") is not True
    assert second.metadata.get("error") is not True
    assert max_active_prompts == 1


@pytest.mark.asyncio
async def test_grant_lock_serializes_same_file(tmp_path):
    manager = PathGrantLockManager()
    target = tmp_path / "shared.txt"

    first = await manager.acquire_request_targets([target])
    second_task = asyncio.create_task(manager.acquire_request_targets([target]))
    await asyncio.sleep(0)

    assert second_task.done() is False
    await first.release()
    second = await asyncio.wait_for(second_task, timeout=1)
    await second.release()


@pytest.mark.asyncio
async def test_grant_lock_sibling_upgrade_to_parent(tmp_path):
    manager = PathGrantLockManager()
    root = tmp_path / "external"
    a = root / "a.txt"
    b = root / "b.txt"
    root.mkdir()

    first = await manager.acquire_request_targets([a])
    sibling = await manager.acquire_request_targets([b])
    parent_task = asyncio.create_task(manager.acquire_request_targets([root]))
    await asyncio.sleep(0)

    assert parent_task.done() is False
    await first.release()
    assert parent_task.done() is False
    await sibling.release()
    parent = await asyncio.wait_for(parent_task, timeout=1)
    await parent.release()


@pytest.mark.asyncio
async def test_concurrent_persistent_grants_merge_latest(tmp_path):
    settings = Settings(str(tmp_path))
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"

    await asyncio.gather(
        asyncio.to_thread(settings.add_persistent_grant_delta, GrantDelta(writable_files=[str(first)])),
        asyncio.to_thread(settings.add_persistent_grant_delta, GrantDelta(writable_files=[str(second)])),
    )

    reloaded = Settings(str(tmp_path))
    assert set(reloaded.get_persistent_writable_files()) == {str(first), str(second)}


@pytest.mark.asyncio
async def test_revision_domains_are_independent(tmp_path):
    service = PermissionService()
    start_state = service.state_revision
    start_permissions = service.permissions_revision

    service.set_permission_preset("project_trusted")
    assert service.state_revision == start_state + 1
    assert service.permissions_revision == start_permissions

    await service.add_grant(AccessGrant(path=str(tmp_path / "allowed.txt"), access="read", object_type="file", persistence="session"))
    assert service.state_revision == start_state + 2
    assert service.permissions_revision == start_permissions

    service.clear_session_permissions()
    assert service.state_revision == start_state + 3
    assert service.permissions_revision == start_permissions


def test_permission_not_ready_blocks_all_authorization_entries(tmp_path):
    service = PermissionService(permission_state_ready=False)
    external = tmp_path / "external.txt"
    external.write_text("blocked\n", encoding="utf-8")
    context = PermissionContext.from_service(service, workspace=str(tmp_path / "workspace"))

    for tool_call in (
        {"name": "read", "args": {"file_path": str(external)}},
        {"name": "write", "args": {"file_path": str(external), "op": "append", "new_string": "x"}},
        {"name": "replace", "args": {"file_path": str(external), "bounds": [{"line_no": 1, "anchor": "blocked"}], "new_string": "x"}},
    ):
        decision = authorize_tool_call(tool_call, context)
        assert decision.action == "deny"
        assert "not ready" in decision.reason.lower()




def test_resolve_access_denies_not_ready_stale_external_grants(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external.txt"
    workspace.mkdir()
    external.write_text("blocked\n", encoding="utf-8")
    grants = AccessGrants.from_parts(
        readable_files=[str(external)],
        permission_state_ready=False,
    )

    resolution = resolve_access(
        str(workspace),
        str(external),
        access="read",
        access_grants=grants,
        require_exists=True,
    )

    assert resolution.action == "deny"
    assert "not ready" in resolution.reason.lower()


@pytest.mark.asyncio
async def test_tool_context_get_access_grants_fails_closed_when_not_ready(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external.txt"
    workspace.mkdir()
    external.write_text("blocked\n", encoding="utf-8")
    service = PermissionService(
        permission_state_ready=False,
        persistent_grants=[AccessGrant(path=str(external), access="read", object_type="file", persistence="persistent")],
    )
    prompted = False

    async def interact(_req: UserInteraction) -> UserResponse:
        nonlocal prompted
        prompted = True
        return UserResponse(value="allow")

    ctx = ToolContext(
        workspace=str(workspace),
        get_access_grants=service.get_access_grants,
        interact=interact,
    )

    result = await ToolRegistry().execute_tool("read", {"file_path": str(external)}, ctx)

    assert result.metadata.get("error") is True
    assert "not ready" in result.output.lower()
    assert prompted is False


@pytest.mark.asyncio
async def test_add_grant_rejects_stale_approval_precondition(tmp_path):
    service = PermissionService(permission_mode="custom", sandbox_mode="workspace-write")
    precondition = ApprovalPrecondition(
        permission_mode=service.permission_mode,
        revocation_epoch=service.revocation_epoch,
    )

    service.set_permission_preset("read_only")
    result = await service.add_grant(
        AccessGrant(path=str(tmp_path / "stale.txt"), access="read", object_type="file", persistence="session"),
        precondition=precondition,
    )

    assert result.ok is False
    assert result.conflict is True
    assert str(tmp_path / "stale.txt") not in service.get_access_grants().readable_files
def test_permission_transaction_postcommit_recovery(tmp_path, monkeypatch):
    settings = Settings(str(tmp_path))
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    settings.add_persistent_grant_delta(GrantDelta(writable_files=[str(first)]))

    original_save = settings._save
    calls = 0

    def flaky_save():
        nonlocal calls
        calls += 1
        original_save()
        if calls == 1:
            raise OSError("postcommit crash")

    monkeypatch.setattr(settings, "_save", flaky_save)

    with pytest.raises(OSError):
        settings.add_persistent_grant_delta(GrantDelta(writable_files=[str(second)]))

    assert set(json.loads(settings._path.read_text(encoding="utf-8"))["persistent_writable_files"]) == {str(first), str(second)}


@pytest.mark.asyncio
async def test_permission_preset_change_preserves_path_grant_sources(tmp_path):
    service = PermissionService(
        sandbox_readable_files=[str(tmp_path / "sandbox.txt")],
        sandbox_writable_dirs=[str(tmp_path / "sandbox-dir")],
    )
    session_file = tmp_path / "session.txt"
    persistent_file = tmp_path / "persistent.txt"
    runtime_file = tmp_path / "runtime.txt"
    await service.add_grant(AccessGrant(path=str(session_file), access="read", object_type="file", persistence="session"))
    await service.add_grant(AccessGrant(path=str(persistent_file), access="read", object_type="file", persistence="persistent"))
    await service.add_grant(AccessGrant(path=str(runtime_file), access="write", object_type="file", persistence="runtime"))
    start_permissions_revision = service.permissions_revision

    service.set_permission_preset("read_only")

    grants = service.get_access_grants()
    assert str(tmp_path / "sandbox.txt") in grants.readable_files
    assert str(tmp_path / "sandbox-dir") in grants.writable_dirs
    assert str(session_file) in grants.readable_files
    assert str(persistent_file) in grants.readable_files
    assert str(runtime_file) in grants.writable_files
    assert service.permissions_revision == start_permissions_revision


def test_persistent_grants_are_stored_separately_from_sandbox_grants(tmp_path):
    settings = Settings(str(tmp_path))
    readable = tmp_path / "readable.txt"
    writable_dir = tmp_path / "writable-dir"

    settings.add_persistent_grant_delta(GrantDelta(readable_files=[str(readable)], writable_dirs=[str(writable_dir)]))

    reloaded = Settings(str(tmp_path))
    assert reloaded.get_sandbox_readable_files() == []
    assert reloaded.get_sandbox_writable_dirs() == []
    assert reloaded.get_persistent_readable_files() == [str(readable)]
    assert reloaded.get_persistent_writable_dirs() == [str(writable_dir)]


@pytest.mark.asyncio
async def test_build_permission_service_hydrates_persistent_grants_separately(tmp_path):
    from voidx.agent.graph.wiring import build_permission_service

    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    target = external / "persisted.txt"
    target.write_text("persisted\n", encoding="utf-8")
    settings = Settings(str(workspace))
    settings.add_persistent_grant_delta(GrantDelta(readable_files=[str(target)]))
    cfg = await (await Settings.create(str(workspace))).build_config()
    cfg.workspace = str(workspace)

    service = build_permission_service(cfg, settings=settings, notifier=lambda _msg: None)

    assert service.sandbox_readable_files == []
    grants = service.get_access_grants()
    assert grants.readable_files == (str(target),)


@pytest.mark.asyncio
async def test_tool_grant_lock_defers_final_target_until_user_choice(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    target = external / "target.txt"
    target.write_text("target\n", encoding="utf-8")
    calls: list[tuple[tuple[str, ...], tuple[str, ...] | None]] = []
    grants: list[AccessGrant] = []

    class _Lock:
        async def release(self) -> None:
            return None

    async def acquire(paths, *, final_paths=None):
        calls.append((tuple(str(Path(p)) for p in paths), None if final_paths is None else tuple(str(Path(p)) for p in final_paths)))
        return _Lock()

    async def interact(_req: UserInteraction) -> UserResponse:
        assert calls[-1][1] is None
        return UserResponse(value="session_file")

    async def add_grant(grant: AccessGrant, *, precondition=None) -> None:
        grants.append(grant)

    ctx = ToolContext(
        workspace=str(workspace),
        get_access_grants=lambda: AccessGrants(),
        add_grant=add_grant,
        acquire_grant_targets=acquire,
        interact=interact,
    )

    result = await ToolRegistry().execute_tool("read", {"file_path": str(target)}, ctx)

    assert result.metadata.get("error") is not True
    assert calls == [((str(target),), None), ((str(target),), (str(target),))]
    assert grants == [AccessGrant(path=str(target), access="read", object_type="file", persistence="session")]


@pytest.mark.asyncio
async def test_manage_external_path_uses_existing_external_grant(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    target = external / "created.txt"
    service = PermissionService()
    await service.add_grant(AccessGrant(path=str(external), access="write", object_type="dir", persistence="session"))
    ctx = ToolContext(
        workspace=str(workspace),
        get_access_grants=service.get_access_grants,
        add_grant=service.add_grant,
        acquire_grant_targets=service.acquire_grant_targets,
        interact=lambda _req: UserResponse(value="deny"),
    )

    result = await ToolRegistry().execute_tool("manage", {"op": "create", "paths": str(target)}, ctx)

    assert result.metadata.get("error") is not True
    assert target.exists()
