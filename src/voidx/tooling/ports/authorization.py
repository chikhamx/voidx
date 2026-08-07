"""Authorization capabilities injected into ToolExecutionContext."""

from __future__ import annotations

from typing import Protocol


class GrantReader(Protocol):
    def get_access_grants(self) -> object: ...
    def get_revocation_epoch(self) -> int: ...


class GrantWriter(Protocol):
    async def add_grant(self, grant: object, precondition: object) -> object: ...


class GrantLock(Protocol):
    async def acquire_grant_targets(self, targets: list[str], **kwargs: object) -> object: ...


class ExecutionLease(Protocol):
    async def release(self) -> None: ...


__all__ = ["GrantReader", "GrantWriter", "GrantLock", "ExecutionLease"]
