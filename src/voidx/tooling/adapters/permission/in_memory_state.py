"""In-memory owner for mutable permission grants, locks, epochs, and leases."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from voidx.tooling.domain.grants import AccessGrant, ObjectType




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


_EXECUTION_LEASE_TOKEN = object()


class ExecutionLease:
    __slots__ = ("_state", "_token", "_released")

    def __init__(self, state: "InMemoryPermissionState", token: object) -> None:
        if token is not _EXECUTION_LEASE_TOKEN:
            raise TypeError("ExecutionLease cannot be constructed directly")
        self._state = state
        self._token = token
        self._released = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._state.release_execution_lease(self)

    async def __aenter__(self) -> "ExecutionLease":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.release()


class InMemoryPermissionState:
    """Own all mutable runtime/session/persistent grant collections."""

    def __init__(self, persistent_grants: list[AccessGrant] | None = None) -> None:
        self.runtime_grants: list[AccessGrant] = []
        self.session_grants: list[AccessGrant] = []
        self.created_path_grants: list[AccessGrant] = []
        self.persistent_grants: list[AccessGrant] = list(persistent_grants or [])
        self.grant_lock_manager = PathGrantLockManager()
        self.commit_lock = asyncio.Lock()
        self.active_execution_leases: set[ExecutionLease] = set()
        self._session_allow: set[str] = set()
        self._session_deny: set[str] = set()
        self.state_revision = 0
        self.permissions_revision = 0
        self.revocation_epoch = 0
        self.ai_approval_count = 0

    def grants_for(self, persistence: str) -> list[AccessGrant]:
        if persistence == "persistent":
            return self.persistent_grants
        if persistence == "runtime":
            return self.runtime_grants
        return self.session_grants

    @asynccontextmanager
    async def commit_guard(self):
        async with self.commit_lock:
            yield

    async def acquire_grant_targets(self, paths, *, final_paths=None) -> object:
        if final_paths is not None:
            return await self.grant_lock_manager.acquire_final_targets(paths, final_paths)
        return await self.grant_lock_manager.acquire_request_targets(paths)

    def acquire_execution_lease(self) -> ExecutionLease:
        lease = ExecutionLease(self, _EXECUTION_LEASE_TOKEN)
        self.active_execution_leases.add(lease)
        return lease

    def has_active_execution_lease(self, lease: ExecutionLease) -> bool:
        return lease in self.active_execution_leases and not lease._released

    def release_execution_lease(self, lease: ExecutionLease) -> None:
        self.active_execution_leases.discard(lease)

    def clear_runtime_grants(self) -> bool:
        if not self.runtime_grants:
            return False
        self.runtime_grants.clear()
        return True


    def session_rules(self) -> tuple[frozenset[str], frozenset[str]]:
        return frozenset(self._session_allow), frozenset(self._session_deny)

    def set_session_rule(self, tool: str, *, allow: bool) -> None:
        target = self._session_allow if allow else self._session_deny
        opposite = self._session_deny if allow else self._session_allow
        target.add(tool)
        opposite.discard(tool)

    def has_active_leases(self) -> bool:
        return bool(self.active_execution_leases)

    def advance_state(self, *, permissions: bool = False, revoke: bool = False) -> None:
        self.state_revision += 1
        if permissions:
            self.permissions_revision += 1
        if revoke:
            self.revocation_epoch += 1

    def grants_snapshot(self) -> tuple[AccessGrant, ...]:
        return tuple((
            *self.runtime_grants,
            *self.session_grants,
            *self.created_path_grants,
            *self.persistent_grants,
        ))

    def created_path_grants_snapshot(self) -> tuple[AccessGrant, ...]:
        return tuple(self.created_path_grants)

    def clear_created_path_grants(self) -> bool:
        if not self.created_path_grants:
            return False
        self.created_path_grants.clear()
        return True

    def add_grant(self, grant: AccessGrant) -> bool:
        target = self.grants_for(grant.persistence)
        if grant in target:
            return False
        target.append(grant)
        self.advance_state(permissions=grant.persistence == "persistent")
        return True

    def record_created_path(self, path: object, *, object_type: ObjectType) -> bool:
        grant = _created_path_grant(path, object_type=object_type)
        if grant in self.created_path_grants:
            return False
        self.created_path_grants.append(grant)
        self.advance_state()
        return True

    def forget_created_path(self, path: object, *, object_type: ObjectType) -> bool:
        target = _normalize_lock_path(path)
        retained = [
            grant
            for grant in self.created_path_grants
            if not _created_grant_is_within(grant, target, object_type=object_type)
        ]
        if len(retained) == len(self.created_path_grants):
            return False
        self.created_path_grants[:] = retained
        self.advance_state()
        return True

    def move_created_path(
        self,
        source: object,
        dest: object,
        *,
        object_type: ObjectType,
        destination_created: bool,
    ) -> bool:
        source_path = _normalize_lock_path(source)
        dest_path = _normalize_lock_path(dest)
        source_owned = any(
            _created_grant_covers(grant, source_path)
            for grant in self.created_path_grants
        )
        moved: list[AccessGrant] = []
        retained: list[AccessGrant] = []
        for grant in self.created_path_grants:
            grant_path = _normalize_lock_path(grant.path)
            if _created_grant_is_within(grant, dest_path, object_type=object_type):
                continue
            if _contains(source_path, grant_path):
                if destination_created:
                    moved.append(
                        _created_path_grant(
                            dest_path / grant_path.relative_to(source_path),
                            object_type=grant.object_type,
                        )
                    )
                continue
            retained.append(grant)
        if destination_created and source_owned and not moved:
            moved.append(_created_path_grant(dest_path, object_type=object_type))
        updated = list(dict.fromkeys((*retained, *moved)))
        if updated == self.created_path_grants:
            return False
        self.created_path_grants[:] = updated
        self.advance_state()
        return True

    def clear_runtime_grants_and_advance(self) -> None:
        if self.has_active_leases():
            return
        if self.clear_runtime_grants():
            self.advance_state()

    def clear_session_permissions(self) -> bool:
        had_permissions = bool(
            self._session_allow
            or self._session_deny
            or self.session_grants
            or self.created_path_grants
            or self.ai_approval_count > 0
        )
        self._session_allow.clear()
        self._session_deny.clear()
        self.session_grants.clear()
        self.created_path_grants.clear()
        self.ai_approval_count = 0
        if had_permissions:
            self.advance_state(revoke=True)
        return had_permissions

    def increment_ai_approval_count(self) -> None:
        self.ai_approval_count += 1
        self.advance_state()
    def set_ai_approval_count(self, value: int) -> None:
        self.ai_approval_count = value


def _created_path_grant(path: object, *, object_type: ObjectType) -> AccessGrant:
    return AccessGrant(
        path=str(_normalize_lock_path(path)),
        access="write",
        object_type=object_type,
        persistence="session",
    )


def _created_grant_covers(grant: AccessGrant, path: Path) -> bool:
    grant_path = _normalize_lock_path(grant.path)
    return path == grant_path or (grant.object_type == "dir" and _contains(grant_path, path))


def _created_grant_is_within(
    grant: AccessGrant,
    root: Path,
    *,
    object_type: ObjectType,
) -> bool:
    grant_path = _normalize_lock_path(grant.path)
    return grant_path == root or (object_type == "dir" and _contains(root, grant_path))


def _normalize_lock_path(path: object) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _paths_overlap(left: Path, right: Path) -> bool:
    return _contains(left, right) or _contains(right, left)


def _contains(base: Path, path: Path) -> bool:
    try:
        return path == base or path.is_relative_to(base)
    except (OSError, ValueError):
        return False


def create_permission_service(*args, **kwargs):
    from voidx.tooling.application.permission_service import PermissionService

    persistent_grants = kwargs.get("persistent_grants")
    state = InMemoryPermissionState(persistent_grants)
    return PermissionService(*args, **kwargs, state=state)


