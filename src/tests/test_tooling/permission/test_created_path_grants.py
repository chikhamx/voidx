"""Session-scoped grants derived from paths created by voidx."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.tool_registry import build_registry
from voidx.tooling.application.execution import (
    AuthorizationRuntime,
    CallbackInteractionPort,
    FileToolContext,
)
from voidx.tooling.domain.interaction import UserResponse

from voidx.tooling.adapters.permission.in_memory_state import create_permission_service as PermissionService
from voidx.tooling.domain.grants import AccessGrant
from voidx.tooling.policy.filesystem.grants import resolve_access


def _resolve(service, workspace: Path, path: Path, *, access: str = "write"):
    return resolve_access(
        str(workspace),
        str(path),
        access=access,
        access_grants=service.get_access_grants(),
        allow_missing_write_file=True,
    )


@pytest.mark.asyncio
async def test_created_file_grant_allows_later_read_and_write(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "external" / "created.txt"

    service = PermissionService()
    await service.record_created_path(target, object_type="file")

    assert _resolve(service, workspace, target, access="read").action == "allow"
    assert _resolve(service, workspace, target).action == "allow"


@pytest.mark.asyncio
async def test_created_directory_grant_allows_descendants(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "external" / "created-dir"
    child = target / "nested" / "child.txt"
    service = PermissionService()

    await service.record_created_path(target, object_type="dir")

    assert _resolve(service, workspace, child).action == "allow"


@pytest.mark.asyncio
async def test_forgetting_created_file_preserves_user_session_grant(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "external" / "created.txt"
    service = PermissionService()
    user_grant = AccessGrant(
        path=str(target.resolve()),
        access="write",
        object_type="file",
        persistence="session",
    )
    await service.add_grant(user_grant)
    await service.record_created_path(target, object_type="file")

    await service.forget_created_path(target, object_type="file")

    assert service.grant_snapshot() == (user_grant,)
    assert _resolve(service, workspace, target).action == "allow"


@pytest.mark.asyncio
async def test_moving_created_directory_migrates_tree_and_replaces_destination_grants(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external" / "source"
    dest = tmp_path / "external" / "dest"
    service = PermissionService()
    await service.record_created_path(source, object_type="dir")
    await service.record_created_path(source / "nested.txt", object_type="file")
    await service.record_created_path(dest / "stale.txt", object_type="file")

    await service.move_created_path(
        source,
        dest,
        object_type="dir",
        destination_created=True,
    )

    assert _resolve(service, workspace, source / "nested.txt").action == "defer"
    assert _resolve(service, workspace, dest / "nested.txt").action == "allow"
    assert _resolve(service, workspace, dest / "stale.txt").action == "allow"
    created_paths = {grant.path for grant in service.created_path_grant_snapshot()}
    assert str((dest / "nested.txt").resolve()) in created_paths
    assert str((dest / "stale.txt").resolve()) not in created_paths


@pytest.mark.asyncio
async def test_created_path_grants_clear_with_session_permissions(tmp_path):
    target = tmp_path / "external" / "created.txt"
    service = PermissionService()
    await service.record_created_path(target, object_type="file")

    service.clear_session_permissions()

    assert service.created_path_grant_snapshot() == ()


@pytest.mark.asyncio
async def test_permission_mode_change_clears_only_created_path_grants(tmp_path):
    user_target = tmp_path / "external" / "user.txt"
    created_target = tmp_path / "external" / "created.txt"
    service = PermissionService(permission_mode="safe")
    user_grant = AccessGrant(
        path=str(user_target.resolve()),
        access="write",
        object_type="file",
        persistence="session",
    )
    await service.add_grant(user_grant)
    await service.record_created_path(created_target, object_type="file")

    service.set_permission_mode("project_trusted")

    assert service.created_path_grant_snapshot() == ()
    assert service.grant_snapshot() == (user_grant,)


def _tool_context(service, workspace: Path, prompts: list[str]) -> FileToolContext:
    def interact(request):
        prompts.append(request.prompt)
        return UserResponse(value="deny")

    return FileToolContext(
        workspace=str(workspace),
        authorization_service=AuthorizationRuntime(
            access_grants_reader=service.get_access_grants,
            grant_writer=service.add_grant,
            target_locker=service.acquire_grant_targets,
            interaction=CallbackInteractionPort(interact),
            created_path_recorder=service.record_created_path,
            created_path_forgetter=service.forget_created_path,
            created_path_mover=service.move_created_path,
        ),
    )


async def _add_runtime_grant(service, path: Path, *, object_type: str) -> None:
    await service.add_grant(
        AccessGrant(
            path=str(path.resolve()),
            access="write",
            object_type=object_type,
            persistence="runtime",
        )
    )


@pytest.mark.asyncio
async def test_manage_created_external_file_remains_accessible_after_runtime_grant_clears(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    target = external / "created.txt"
    service = PermissionService()
    prompts: list[str] = []
    ctx = _tool_context(service, workspace, prompts)
    registry = build_registry()
    await _add_runtime_grant(service, target, object_type="file")

    async with service.execution_lease_for_tool("manage"):
        created = await registry.execute_tool(
            "manage",
            {"op": "create", "kind": "file", "paths": str(target)},
            ctx,
        )

    written = await registry.execute_tool(
        "write",
        {"file_path": str(target), "op": "write", "new_string": "created\n"},
        ctx,
    )
    read = await registry.execute_tool("read", {"file_path": str(target)}, ctx)

    assert created.metadata.get("error") is not True
    assert written.metadata.get("error") is not True
    assert read.metadata.get("error") is not True
    assert prompts == []
    assert service.created_path_grant_snapshot() == (
        AccessGrant(str(target.resolve()), "write", "file", "session"),
    )


@pytest.mark.asyncio
async def test_manage_created_external_directory_allows_child_creation(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    target = external / "created-dir"
    child = target / "child.txt"
    service = PermissionService()
    prompts: list[str] = []
    ctx = _tool_context(service, workspace, prompts)
    registry = build_registry()
    await _add_runtime_grant(service, target, object_type="dir")

    async with service.execution_lease_for_tool("manage"):
        created = await registry.execute_tool(
            "manage",
            {"op": "create", "kind": "dir", "paths": str(target)},
            ctx,
        )
    written = await registry.execute_tool(
        "write",
        {"file_path": str(child), "op": "write", "new_string": "child\n"},
        ctx,
    )

    assert created.metadata.get("error") is not True
    assert written.metadata.get("error") is not True
    assert child.read_text(encoding="utf-8") == "child\n"
    assert prompts == []


@pytest.mark.asyncio
async def test_write_created_external_file_records_grant_but_failure_and_overwrite_do_not(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    created_target = external / "created.txt"
    existing_target = external / "existing.txt"
    failed_parent = external / "not-a-directory"
    failed_target = failed_parent / "failed.txt"
    existing_target.write_text("before\n", encoding="utf-8")
    failed_parent.write_text("parent is a file\n", encoding="utf-8")
    service = PermissionService()
    prompts: list[str] = []
    ctx = _tool_context(service, workspace, prompts)
    registry = build_registry()

    await _add_runtime_grant(service, created_target, object_type="file")
    async with service.execution_lease_for_tool("write"):
        created = await registry.execute_tool(
            "write",
            {"file_path": str(created_target), "op": "write", "new_string": "created\n"},
            ctx,
        )
    await _add_runtime_grant(service, existing_target, object_type="file")
    async with service.execution_lease_for_tool("write"):
        overwritten = await registry.execute_tool(
            "write",
            {"file_path": str(existing_target), "op": "write", "new_string": "after\n"},
            ctx,
        )
    await _add_runtime_grant(service, failed_target, object_type="file")
    async with service.execution_lease_for_tool("write"):
        failed = await registry.execute_tool(
            "write",
            {"file_path": str(failed_target), "op": "write", "new_string": "failed\n"},
            ctx,
        )

    assert created.metadata.get("error") is not True
    assert overwritten.metadata.get("error") is not True
    assert failed.metadata.get("error") is True
    assert service.created_path_grant_snapshot() == (
        AccessGrant(str(created_target.resolve()), "write", "file", "session"),
    )
    assert prompts == []


@pytest.mark.asyncio
async def test_manage_delete_revokes_and_move_migrates_created_file_grants(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    deleted_target = external / "deleted.txt"
    source = external / "source.txt"
    dest = external / "dest.txt"
    service = PermissionService()
    prompts: list[str] = []
    ctx = _tool_context(service, workspace, prompts)
    registry = build_registry()

    for target in (deleted_target, source):
        await _add_runtime_grant(service, target, object_type="file")
        async with service.execution_lease_for_tool("manage"):
            result = await registry.execute_tool(
                "manage",
                {"op": "create", "kind": "file", "paths": str(target)},
                ctx,
            )
        assert result.metadata.get("error") is not True

    deleted = await registry.execute_tool(
        "manage",
        {"op": "delete", "kind": "file", "paths": str(deleted_target)},
        ctx,
    )
    await _add_runtime_grant(service, dest, object_type="file")
    async with service.execution_lease_for_tool("manage"):
        moved = await registry.execute_tool(
            "manage",
            {
                "op": "move",
                "kind": "file",
                "moves": [{"src": str(source), "dest": str(dest), "overwrite": False}],
            },
            ctx,
        )

    assert deleted.metadata.get("error") is not True
    assert moved.metadata.get("error") is not True
    assert _resolve(service, workspace, deleted_target).action == "defer"
    assert _resolve(service, workspace, source).action == "defer"
    assert _resolve(service, workspace, dest).action == "allow"
    assert service.created_path_grant_snapshot() == (
        AccessGrant(str(dest.resolve()), "write", "file", "session"),
    )
    assert prompts == []


@pytest.mark.asyncio
async def test_manage_directory_tool_approval_grants_exact_new_directory(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    target = external / "approved-dir"
    service = PermissionService()

    def interact(_request):
        return UserResponse(value="session_dir")

    ctx = FileToolContext(
        workspace=str(workspace),
        authorization_service=AuthorizationRuntime(
            access_grants_reader=service.get_access_grants,
            grant_writer=service.add_grant,
            target_locker=service.acquire_grant_targets,
            interaction=CallbackInteractionPort(interact),
        ),
    )

    result = await build_registry().execute_tool(
        "manage",
        {"op": "create", "kind": "dir", "paths": str(target)},
        ctx,
    )

    assert result.metadata.get("error") is not True
    assert service.grant_snapshot() == (
        AccessGrant(str(target.resolve()), "write", "dir", "session"),
    )


@pytest.mark.asyncio
async def test_manage_move_from_workspace_records_external_destination(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    source = workspace / "source.txt"
    dest = external / "dest.txt"
    source.write_text("source\n", encoding="utf-8")
    service = PermissionService()
    prompts: list[str] = []
    ctx = _tool_context(service, workspace, prompts)
    await _add_runtime_grant(service, dest, object_type="file")

    async with service.execution_lease_for_tool("manage"):
        moved = await build_registry().execute_tool(
            "manage",
            {
                "op": "move",
                "kind": "file",
                "moves": [{"src": str(source), "dest": str(dest), "overwrite": False}],
            },
            ctx,
        )

    assert moved.metadata.get("error") is not True
    assert service.created_path_grant_snapshot() == (
        AccessGrant(str(dest.resolve()), "write", "file", "session"),
    )
    assert prompts == []


@pytest.mark.asyncio
async def test_manage_move_from_workspace_over_existing_external_file_does_not_record_destination(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    source = workspace / "source.txt"
    dest = external / "existing.txt"
    source.write_text("source\n", encoding="utf-8")
    dest.write_text("existing\n", encoding="utf-8")
    service = PermissionService()
    prompts: list[str] = []
    ctx = _tool_context(service, workspace, prompts)
    await _add_runtime_grant(service, dest, object_type="file")

    async with service.execution_lease_for_tool("manage"):
        moved = await build_registry().execute_tool(
            "manage",
            {
                "op": "move",
                "kind": "file",
                "moves": [{"src": str(source), "dest": str(dest), "overwrite": True}],
            },
            ctx,
        )

    assert moved.metadata.get("error") is not True
    assert service.created_path_grant_snapshot() == ()
    assert prompts == []


@pytest.mark.asyncio
async def test_manage_move_created_external_file_over_existing_external_file_does_not_transfer_grant(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    source = external / "created-source.txt"
    dest = external / "existing-dest.txt"
    dest.write_text("existing\n", encoding="utf-8")
    service = PermissionService()
    prompts: list[str] = []
    ctx = _tool_context(service, workspace, prompts)
    await _add_runtime_grant(service, source, object_type="file")

    async with service.execution_lease_for_tool("manage"):
        created = await build_registry().execute_tool(
            "manage",
            {"op": "create", "kind": "file", "paths": str(source)},
            ctx,
        )
    await _add_runtime_grant(service, dest, object_type="file")
    async with service.execution_lease_for_tool("manage"):
        moved = await build_registry().execute_tool(
            "manage",
            {
                "op": "move",
                "kind": "file",
                "moves": [{"src": str(source), "dest": str(dest), "overwrite": True}],
            },
            ctx,
        )

    assert created.metadata.get("error") is not True
    assert moved.metadata.get("error") is not True
    assert service.created_path_grant_snapshot() == ()
    assert prompts == []


@pytest.mark.asyncio
async def test_write_created_external_file_does_not_record_grant_when_final_state_unavailable(tmp_path):
    class RemovingFormatter:
        enabled = True

        async def format_range(self, file_path, _range):
            Path(file_path).unlink()
            raise RuntimeError("removed final file")

    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    target = external / "created-then-removed.txt"
    service = PermissionService()
    prompts: list[str] = []
    ctx = _tool_context(service, workspace, prompts).model_copy(
        update={"post_edit_formatter": RemovingFormatter()}
    )
    await _add_runtime_grant(service, target, object_type="file")

    async with service.execution_lease_for_tool("write"):
        result = await build_registry().execute_tool(
            "write",
            {"file_path": str(target), "op": "write", "new_string": "created\n"},
            ctx,
        )

    assert result.metadata.get("error") is True
    assert service.created_path_grant_snapshot() == ()
    assert prompts == []
