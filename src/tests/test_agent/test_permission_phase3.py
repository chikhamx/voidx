"""Phase 3 SafePathExecutor and manage tool tests."""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from voidx.tools.base import ToolContext, UserInteraction, UserResponse
from voidx.tools.registry import ToolRegistry


@pytest.mark.asyncio
async def test_manage_create_external_file_uses_tool_approval(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    target = external / "created.txt"
    seen_request: UserInteraction | None = None

    async def interact(req: UserInteraction) -> UserResponse:
        nonlocal seen_request
        seen_request = req
        return UserResponse(value="allow")

    ctx = ToolContext(workspace=str(workspace), interact=interact)

    result = await ToolRegistry().execute_tool("manage", {"op": "create", "paths": str(target)}, ctx)

    assert result.metadata.get("error") is not True
    assert result.metadata["succeeded"] == 1
    assert target.exists()
    assert seen_request is not None
    assert seen_request.prompt == f"Write file outside workspace? {target}"


@pytest.mark.asyncio
async def test_move_source_requires_write(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    source = external / "source.txt"
    dest = workspace / "dest.txt"
    source.write_text("source\n", encoding="utf-8")
    seen_request: UserInteraction | None = None

    async def interact(req: UserInteraction) -> UserResponse:
        nonlocal seen_request
        seen_request = req
        return UserResponse(value="deny")

    ctx = ToolContext(
        workspace=str(workspace),
        sandbox_readable_files=[str(source)],
        interact=interact,
    )

    result = await ToolRegistry().execute_tool(
        "manage",
        {"op": "move", "moves": [{"src": str(source), "dest": "dest.txt"}]},
        ctx,
    )

    assert result.metadata["failed"] == 1
    assert seen_request is not None
    assert source.exists()
    assert not dest.exists()


@pytest.mark.asyncio
async def test_move_cross_write_grants(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    source = external / "source.txt"
    dest = external / "dest.txt"
    source.write_text("source\n", encoding="utf-8")
    prompts: list[UserInteraction] = []

    async def interact(req: UserInteraction) -> UserResponse:
        prompts.append(req)
        return UserResponse(value="allow")

    ctx = ToolContext(workspace=str(workspace), interact=interact)

    result = await ToolRegistry().execute_tool(
        "manage",
        {"op": "move", "moves": [{"src": str(source), "dest": str(dest)}]},
        ctx,
    )

    assert result.metadata.get("error") is not True
    assert result.metadata["succeeded"] == 1
    assert not source.exists()
    assert dest.read_text(encoding="utf-8") == "source\n"
    assert [prompt.prompt for prompt in prompts] == [
        f"Write file outside workspace? {source}",
        f"Write file outside workspace? {dest}",
    ]


def test_authorized_path_is_unforgeable(tmp_path):
    from voidx.tools.file.safe_path import AuthorizedPath

    with pytest.raises(TypeError):
        AuthorizedPath(tmp_path / "target.txt")


def test_safe_path_read_rejects_symlink_swap(tmp_path):
    from voidx.tools.file.safe_path import SafePathExecutor

    target = tmp_path / "target.txt"
    replacement = tmp_path / "replacement.txt"
    target.write_text("safe\n", encoding="utf-8")
    replacement.write_text("unsafe\n", encoding="utf-8")
    executor = SafePathExecutor()
    authorized = executor.authorize_existing(target, access="read")

    target.unlink()
    target.symlink_to(replacement)

    result = executor.read_text(authorized)

    assert result.ok is False
    assert result.error_kind == "path_changed"


def test_safe_path_rejects_cross_filesystem_move(tmp_path, monkeypatch):
    from voidx.tools.file.safe_path import SafePathExecutor

    source = tmp_path / "source.txt"
    dest = tmp_path / "dest.txt"
    source.write_text("source\n", encoding="utf-8")
    executor = SafePathExecutor()
    authorized_source = executor.authorize_existing(source, access="write")
    authorized_dest = executor.authorize_target(dest, access="write")

    def raise_exdev(_source: str | bytes | os.PathLike, _dest: str | bytes | os.PathLike, *args, **kwargs) -> None:
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(os, "link", raise_exdev)

    result = executor.rename(authorized_source, authorized_dest, overwrite=False)

    assert result.ok is False
    assert result.error_kind == "cross_device_move"
    assert source.exists()
    assert not dest.exists()


class _ForgedAuthorizedPath:
    def __init__(self, path):
        self.path = path
        self.access = "write"
        self._stat = None


def test_safe_path_rejects_forged_capability(tmp_path):
    from voidx.tools.file.safe_path import SafePathExecutor

    target = tmp_path / "forged.txt"
    forged = _ForgedAuthorizedPath(target)

    result = SafePathExecutor().write_text(forged, "forged\n")  # type: ignore[arg-type]

    assert result.ok is False
    assert result.error_kind == "invalid_capability"
    assert not target.exists()


def test_safe_path_rejects_cross_executor_capability(tmp_path):
    from voidx.tools.file.safe_path import SafePathExecutor

    target = tmp_path / "cross.txt"
    authorized = SafePathExecutor().authorize_target(target, access="write")

    result = SafePathExecutor().write_text(authorized, "cross\n")

    assert result.ok is False
    assert result.error_kind == "invalid_capability"
    assert not target.exists()


def test_authorized_path_is_immutable_after_issue(tmp_path):
    from voidx.tools.file.safe_path import SafePathExecutor

    target = tmp_path / "target.txt"
    target.write_text("ok\n", encoding="utf-8")
    authorized = SafePathExecutor().authorize_existing(target, access="write")

    with pytest.raises(AttributeError):
        authorized.path = tmp_path / "other.txt"


def test_safe_path_write_rejects_parent_symlink_swap(tmp_path):
    from voidx.tools.file.safe_path import SafePathExecutor

    parent = tmp_path / "approved"
    outside = tmp_path / "outside"
    parent.mkdir()
    outside.mkdir()
    target = parent / "new.txt"
    executor = SafePathExecutor()
    authorized = executor.authorize_target(target, access="write")

    parent.rmdir()
    parent.symlink_to(outside, target_is_directory=True)

    result = executor.write_text(authorized, "secret\n")

    assert result.ok is False
    assert result.error_kind == "path_changed"
    assert not (outside / "new.txt").exists()




def test_safe_path_write_text_preserves_existing_file_when_commit_fails(tmp_path, monkeypatch):
    from voidx.tools.file.safe_path import SafePathExecutor

    target = tmp_path / "target.txt"
    target.write_text("old\n", encoding="utf-8")
    executor = SafePathExecutor()
    authorized = executor.authorize_existing(target, access="write")

    def fail_replace(*args, **kwargs):
        raise OSError(errno.EIO, "commit failed")

    monkeypatch.setattr(os, "replace", fail_replace)

    result = executor.write_text(authorized, "new\n")

    assert result.ok is False
    assert target.read_text(encoding="utf-8") == "old\n"
@pytest.mark.asyncio
async def test_read_binary_probe_symlink_swap_fails_closed(tmp_path, monkeypatch):
    import voidx.tools.file.read as read_module

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    target = workspace / "target.txt"
    replacement = outside / "replacement.txt"
    target.write_text("safe\n", encoding="utf-8")
    replacement.write_text("unsafe\n", encoding="utf-8")

    def swap_during_binary_probe(executor, authorized):
        authorized.path.unlink()
        authorized.path.symlink_to(replacement)
        return False, None

    monkeypatch.setattr(read_module, "_binary_null_byte_detected", swap_during_binary_probe)

    result = await ToolRegistry().execute_tool("read", {"file_path": "target.txt"}, ToolContext(workspace=str(workspace)))

    assert result.metadata.get("error") is True
    assert "unsafe" not in result.output


@pytest.mark.asyncio
async def test_directory_overwrite_exdev_preserves_destination(tmp_path, monkeypatch):
    source_child = tmp_path / "source" / "app.py"
    dest_child = tmp_path / "dest" / "old.py"
    source_child.parent.mkdir()
    dest_child.parent.mkdir()
    source_child.write_text("source\n", encoding="utf-8")
    dest_child.write_text("dest\n", encoding="utf-8")

    def raise_exdev(_source, _dest):
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(os, "replace", raise_exdev)

    result = await ToolRegistry().execute_tool(
        "manage",
        {"op": "move", "kind": "dir", "moves": [{"src": "source", "dest": "dest", "overwrite": True}]},
        ToolContext(workspace=str(tmp_path)),
    )

    assert result.metadata["results"][0]["status"] == "error"
    assert result.metadata["results"][0].get("error_kind") == "cross_device_move"
    assert source_child.read_text(encoding="utf-8") == "source\n"
    assert dest_child.read_text(encoding="utf-8") == "dest\n"


def test_safe_path_no_overwrite_move_rejects_destination_race(tmp_path, monkeypatch):
    from voidx.tools.file.safe_path import SafePathExecutor

    source = tmp_path / "source.txt"
    dest = tmp_path / "dest.txt"
    source.write_text("source\n", encoding="utf-8")
    def racing_link(src, dst, *args, **kwargs):
        Path(dst).write_text("raced\n", encoding="utf-8")
        raise FileExistsError

    monkeypatch.setattr(os, "link", racing_link)
    executor = SafePathExecutor()
    authorized_source = executor.authorize_existing(source, access="write")
    authorized_dest = executor.authorize_target(dest, access="write")

    result = executor.rename(authorized_source, authorized_dest, overwrite=False)

    assert result.ok is False
    assert result.error_kind == "destination_exists"
    assert source.read_text(encoding="utf-8") == "source\n"
    assert dest.read_text(encoding="utf-8") == "raced\n"


def test_safe_path_delete_tree_rejects_child_symlink_race(tmp_path, monkeypatch):
    import shutil
    import voidx.tools.file.safe_path as safe_path
    from voidx.tools.file.safe_path import SafePathExecutor

    root = tmp_path / "root"
    child = root / "child"
    outside = tmp_path / "outside"
    child.mkdir(parents=True)
    outside.mkdir()
    (child / "inside.txt").write_text("inside\n", encoding="utf-8")
    secret = outside / "secret.txt"
    secret.write_text("secret\n", encoding="utf-8")
    original_stat = safe_path.os.stat
    swapped = False

    def racing_stat(path, *args, **kwargs):
        nonlocal swapped
        result = original_stat(path, *args, **kwargs)
        if path == child.name and kwargs.get("dir_fd") is not None and not swapped:
            shutil.rmtree(child)
            child.symlink_to(outside, target_is_directory=True)
            swapped = True
        return result

    monkeypatch.setattr(safe_path.os, "stat", racing_stat)
    executor = SafePathExecutor()
    authorized = executor.authorize_existing(root, access="write")

    result = executor.delete_tree(authorized)

    assert result.ok is False
    assert result.error_kind == "path_changed"
    assert secret.read_text(encoding="utf-8") == "secret\n"


def test_safe_path_write_rejects_intermediate_parent_symlink_race(tmp_path, monkeypatch):
    import shutil
    import voidx.tools.file.safe_path as safe_path
    from voidx.tools.file.safe_path import SafePathExecutor

    approved = tmp_path / "approved"
    outside = tmp_path / "outside"
    approved.mkdir()
    outside.mkdir()
    intermediate = approved / "a"
    target = intermediate / "b" / "new.txt"
    original_stat = safe_path._stat_no_symlink
    swapped = False

    def racing_stat(path):
        nonlocal swapped
        result = original_stat(path)
        if Path(path) == intermediate and result is not None and not swapped:
            shutil.rmtree(intermediate)
            intermediate.symlink_to(outside, target_is_directory=True)
            swapped = True
        return result

    monkeypatch.setattr(safe_path, "_stat_no_symlink", racing_stat)
    executor = SafePathExecutor()
    authorized = executor.authorize_target(target, access="write")

    result = executor.write_text(authorized, "secret\n")

    assert result.ok is False
    assert result.error_kind == "path_changed"
    assert not (outside / "b" / "new.txt").exists()


@pytest.mark.asyncio
async def test_manage_move_directory_default_no_overwrite_succeeds_when_dest_absent(tmp_path):
    source_child = tmp_path / "source" / "app.py"
    source_child.parent.mkdir()
    source_child.write_text("source\n", encoding="utf-8")

    result = await ToolRegistry().execute_tool(
        "manage",
        {"op": "move", "kind": "dir", "moves": [{"src": "source", "dest": "dest"}]},
        ToolContext(workspace=str(tmp_path)),
    )

    assert result.metadata["results"][0]["status"] == "moved"
    assert not source_child.parent.exists()
    assert (tmp_path / "dest" / "app.py").read_text(encoding="utf-8") == "source\n"
