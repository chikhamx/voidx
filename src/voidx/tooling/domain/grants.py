"""Canonical path grant resolution for workspace external access."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


__all__ = [
    "AccessGrant",
    "GrantDelta",
    "ApprovalPrecondition",
    "GrantUpdateResult",
    "PermissionCommitResult",
    "PermissionUpdateResult",
    "AccessGrants",
    "AccessIntent",
    "AccessResolution",
]


AccessAction = Literal["allow", "deny", "defer"]
AccessMode = Literal["read", "write"]
GrantPersistence = Literal["runtime", "session", "persistent"]
ObjectType = Literal["file", "dir"]


@dataclass(frozen=True)
class GrantDelta:
    readable_files: list[str] = field(default_factory=list)
    readable_dirs: list[str] = field(default_factory=list)
    writable_files: list[str] = field(default_factory=list)
    writable_dirs: list[str] = field(default_factory=list)


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


