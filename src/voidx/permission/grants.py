"""Canonical path grant resolution for workspace external access."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from voidx.config.grants import GrantDelta

__all__ = [
    "AccessGrant",
    "GrantDelta",
    "ApprovalPrecondition",
    "GrantUpdateResult",
    "PermissionCommitResult",
    "PermissionUpdateResult",
    "PermissionEpochGate",
    "AccessGrants",
    "AccessIntent",
    "AccessResolution",
    "PathGrantLock",
    "PathGrantLockManager",
    "persistent_grants_from_paths",
    "resolve_access",
    "grant_for_intent",
    "delta_for_grant",
]


AccessAction = Literal["allow", "deny", "defer"]
AccessMode = Literal["read", "write"]
GrantPersistence = Literal["runtime", "session", "persistent"]
ObjectType = Literal["file", "dir"]


@dataclass(frozen=True)
class AccessGrant:
    path: str
    access: AccessMode
    object_type: ObjectType
    persistence: GrantPersistence = "session"




@dataclass(frozen=True)
class ApprovalPrecondition:
    permission_mode: str
    revocation_epoch: int


@dataclass(frozen=True)
class GrantUpdateResult:
    persistent: bool
    ok: bool
    committed: bool
    durable: bool | None
    applied: bool
    conflict: bool
    restart_required: bool
    state_revision: int
    permissions_revision: int
    warning: str = ""
    error: str = ""


@dataclass(frozen=True)
class PermissionCommitResult:
    committed: bool
    durable: bool
    conflict: bool
    snapshot: object | None
    latest_snapshot: object | None = None
    warning: str = ""
    error: str = ""


@dataclass(frozen=True)
class PermissionUpdateResult:
    ok: bool
    committed: bool
    durable: bool
    applied: bool
    conflict: bool
    restart_required: bool
    persistent_snapshot: object | None
    latest_snapshot: object | None
    state_revision: int
    permissions_revision: int
    warning: str = ""
    error: str = ""


class PermissionEpochGate:
    def __init__(self) -> None:
        self.epoch = 0

    def advance(self) -> int:
        self.epoch += 1
        return self.epoch


@dataclass(frozen=True)
class AccessGrants:
    readable_files: tuple[str, ...] = ()
    readable_dirs: tuple[str, ...] = ()
    writable_files: tuple[str, ...] = ()
    writable_dirs: tuple[str, ...] = ()
    permissions_revision: int = 0
    state_revision: int = 0
    revocation_epoch: int = 0
    permission_state_ready: bool = True
    permission_mode: str = ""


    @classmethod
    def from_parts(
        cls,
        *,
        readable_files: tuple[str, ...] | list[str] = (),
        readable_dirs: tuple[str, ...] | list[str] = (),
        writable_files: tuple[str, ...] | list[str] = (),
        writable_dirs: tuple[str, ...] | list[str] = (),
        extra_grants: tuple[AccessGrant, ...] | list[AccessGrant] = (),
        permissions_revision: int = 0,
        state_revision: int = 0,
        revocation_epoch: int = 0,
        permission_state_ready: bool = True,
        permission_mode: str = "",
    ) -> "AccessGrants":
        rf = [*readable_files]
        rd = [*readable_dirs]
        wf = [*writable_files]
        wd = [*writable_dirs]
        for grant in extra_grants:
            if grant.access == "read" and grant.object_type == "file":
                rf.append(grant.path)
            elif grant.access == "read" and grant.object_type == "dir":
                rd.append(grant.path)
            elif grant.access == "write" and grant.object_type == "file":
                wf.append(grant.path)
            elif grant.access == "write" and grant.object_type == "dir":
                wd.append(grant.path)
        return cls(
            readable_files=tuple(dict.fromkeys(rf)),
            readable_dirs=tuple(dict.fromkeys(rd)),
            writable_files=tuple(dict.fromkeys(wf)),
            writable_dirs=tuple(dict.fromkeys(wd)),
            permissions_revision=permissions_revision,
            state_revision=state_revision,
            revocation_epoch=revocation_epoch,
            permission_state_ready=permission_state_ready,
            permission_mode=permission_mode,
        )

    def with_delta(self, delta: GrantDelta, *, permissions_revision: int | None = None) -> "AccessGrants":
        return AccessGrants.from_parts(
            readable_files=[*self.readable_files, *delta.readable_files],
            readable_dirs=[*self.readable_dirs, *delta.readable_dirs],
            writable_files=[*self.writable_files, *delta.writable_files],
            writable_dirs=[*self.writable_dirs, *delta.writable_dirs],
            permissions_revision=self.permissions_revision if permissions_revision is None else permissions_revision,
            state_revision=self.state_revision,
            revocation_epoch=self.revocation_epoch,
            permission_state_ready=self.permission_state_ready,
            permission_mode=self.permission_mode,
        )



EffectiveAccessGrants = AccessGrants

@dataclass(frozen=True)
class AccessIntent:
    requested_path: str
    normalized_path: Path
    access: AccessMode
    object_type: ObjectType
    is_workspace_path: bool
    grant_matched: bool


@dataclass(frozen=True)
class AccessResolution:
    action: AccessAction
    intent: AccessIntent | None = None
    reason: str = ""


class PathGrantLock:
    def __init__(self, manager: "PathGrantLockManager", paths: tuple[Path, ...]) -> None:
        self._manager = manager
        self._paths = paths
        self._released = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._manager._release(self._paths)


class PathGrantLockManager:
    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._locked: set[Path] = set()

    async def acquire_request_targets(self, paths: list[str | Path] | tuple[str | Path, ...]) -> PathGrantLock:
        normalized = tuple(_normalize_lock_path(path) for path in paths)
        async with self._condition:
            await self._condition.wait_for(lambda: not self._conflicts(normalized))
            self._locked.update(normalized)
        return PathGrantLock(self, normalized)

    async def acquire_final_targets(
        self,
        paths: list[str | Path] | tuple[str | Path, ...],
        final_paths: list[str | Path] | tuple[str | Path, ...],
    ) -> PathGrantLock:
        normalized = tuple(dict.fromkeys((
            *(_normalize_lock_path(path) for path in paths),
            *(_normalize_lock_path(path) for path in final_paths),
        )))
        async with self._condition:
            await self._condition.wait_for(lambda: not self._conflicts(normalized))
            self._locked.update(normalized)
        return PathGrantLock(self, normalized)

    def _conflicts(self, paths: tuple[Path, ...]) -> bool:
        return any(_paths_overlap(path, locked) for path in paths for locked in self._locked)

    async def _release(self, paths: tuple[Path, ...]) -> None:
        async with self._condition:
            for path in paths:
                self._locked.discard(path)
            self._condition.notify_all()


def resolve_access(
    workspace: str,
    file_path: str,
    *,
    access: AccessMode,
    readable_files: tuple[str, ...] | list[str] = (),
    readable_dirs: tuple[str, ...] | list[str] = (),
    writable_files: tuple[str, ...] | list[str] = (),
    writable_dirs: tuple[str, ...] | list[str] = (),
    access_grants: AccessGrants | None = None,
    require_exists: bool = False,
    allow_missing_write_file: bool = False,
) -> AccessResolution:
    """Resolve a tool path against workspace and grants without side effects."""
    normalized = _normalize_path(workspace, file_path)
    if normalized is None:
        return AccessResolution("deny", reason=f"Path traversal blocked: {file_path}")

    workspace_path = Path(workspace).expanduser().resolve()
    is_workspace_path = _contains(workspace_path, normalized)
    object_type: ObjectType = "dir" if normalized.is_dir() else "file"
    intent = AccessIntent(
        requested_path=file_path,
        normalized_path=normalized,
        access=access,
        object_type=object_type,
        is_workspace_path=is_workspace_path,
        grant_matched=False,
    )

    grants = access_grants or AccessGrants.from_parts(
        readable_files=readable_files,
        readable_dirs=readable_dirs,
        writable_files=writable_files,
        writable_dirs=writable_dirs,
    )

    if is_workspace_path:
        return AccessResolution("allow", intent=intent)

    if not grants.permission_state_ready:
        return AccessResolution("deny", intent=intent, reason="Permission state not ready.")

    if require_exists and not normalized.exists():
        return AccessResolution("defer", intent=intent, reason=f"File not found; external path deferred: {file_path}")
    if access == "write" and not normalized.exists() and not allow_missing_write_file:
        return AccessResolution("defer", intent=intent, reason=f"Path does not exist; external path deferred: {file_path}")

    grants = access_grants or AccessGrants.from_parts(
        readable_files=readable_files,
        readable_dirs=readable_dirs,
        writable_files=writable_files,
        writable_dirs=writable_dirs,
    )
    if _matches_grant(normalized, access, grants.readable_files, grants.readable_dirs, grants.writable_files, grants.writable_dirs):
        return AccessResolution(
            "allow",
            intent=AccessIntent(
                requested_path=file_path,
                normalized_path=normalized,
                access=access,
                object_type=object_type,
                is_workspace_path=False,
                grant_matched=True,
            ),
        )

    return AccessResolution(
        "defer",
        intent=intent,
        reason=f"Permission deferred to tool: {file_path}",
    )


def grant_for_intent(intent: AccessIntent, persistence: GrantPersistence, *, object_type: ObjectType | None = None) -> AccessGrant:
    selected_type = object_type or intent.object_type
    path = intent.normalized_path if selected_type == "file" else _grant_dir_for_intent(intent)
    return AccessGrant(
        path=str(path),
        access=intent.access,
        object_type=selected_type,
        persistence=persistence,
    )


def persistent_grants_from_paths(
    readable_files: list[str],
    readable_dirs: list[str],
    writable_files: list[str],
    writable_dirs: list[str],
) -> list[AccessGrant]:
    return [
        *(AccessGrant(path=path, access="read", object_type="file", persistence="persistent") for path in readable_files),
        *(AccessGrant(path=path, access="read", object_type="dir", persistence="persistent") for path in readable_dirs),
        *(AccessGrant(path=path, access="write", object_type="file", persistence="persistent") for path in writable_files),
        *(AccessGrant(path=path, access="write", object_type="dir", persistence="persistent") for path in writable_dirs),
    ]


def delta_for_grant(grant: AccessGrant) -> GrantDelta:
    if grant.access == "read" and grant.object_type == "file":
        return GrantDelta(readable_files=[grant.path])
    if grant.access == "read" and grant.object_type == "dir":
        return GrantDelta(readable_dirs=[grant.path])
    if grant.access == "write" and grant.object_type == "file":
        return GrantDelta(writable_files=[grant.path])
    return GrantDelta(writable_dirs=[grant.path])


def _grant_dir_for_intent(intent: AccessIntent) -> Path:
    if intent.normalized_path.is_dir():
        return intent.normalized_path
    return intent.normalized_path.parent


def _normalize_path(workspace: str, file_path: str) -> Path | None:
    if not file_path:
        return None
    try:
        workspace_path = Path(workspace).expanduser().resolve()
        raw = Path(file_path)
        if file_path.startswith("~") or raw.is_absolute():
            return raw.expanduser().resolve(strict=False)
        return (workspace_path / raw).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None


def _matches_grant(
    path: Path,
    access: AccessMode,
    readable_files: tuple[str, ...] | list[str],
    readable_dirs: tuple[str, ...] | list[str],
    writable_files: tuple[str, ...] | list[str],
    writable_dirs: tuple[str, ...] | list[str],
) -> bool:
    file_grants = [*writable_files]
    dir_grants = [*writable_dirs]
    if access == "read":
        file_grants.extend(readable_files)
        dir_grants.extend(readable_dirs)
    return _matches_file_grants(path, file_grants) or _matches_dir_grants(path, dir_grants)


def _matches_file_grants(path: Path, grants: tuple[str, ...] | list[str]) -> bool:
    return any(path == grant for grant in _normalized_grants(grants))


def _matches_dir_grants(path: Path, grants: tuple[str, ...] | list[str]) -> bool:
    return any(_contains(grant, path) for grant in _normalized_grants(grants))


def _normalized_grants(grants: tuple[str, ...] | list[str]) -> list[Path]:
    paths: list[Path] = []
    for grant in grants:
        try:
            paths.append(Path(grant).expanduser().resolve(strict=False))
        except (OSError, RuntimeError, ValueError):
            continue
    return paths


def _normalize_lock_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _paths_overlap(left: Path, right: Path) -> bool:
    return _contains(left, right) or _contains(right, left)


def _contains(base: Path, path: Path) -> bool:
    try:
        return path == base or path.is_relative_to(base)
    except (OSError, ValueError):
        return False
