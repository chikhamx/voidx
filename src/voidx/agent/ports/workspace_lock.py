"""Workspace write serialization port."""

from __future__ import annotations

from typing import Protocol


class WorkspaceWriteLockPort(Protocol):
    async def acquire_workspace_write_lock(self, thread_id: str) -> bool: ...
    def release_workspace_write_lock(self, thread_id: str) -> None: ...



class WorkspaceWriteLockBinder(Protocol):
    def bind(self, delegate: WorkspaceWriteLockPort | None) -> None: ...

class NullWorkspaceWriteLock:
    async def acquire_workspace_write_lock(self, thread_id: str) -> bool:
        return True

    def release_workspace_write_lock(self, thread_id: str) -> None:
        return None


class DelegatingWorkspaceWriteLock:
    def __init__(self) -> None:
        self._delegate: WorkspaceWriteLockPort = NullWorkspaceWriteLock()

    def bind(self, delegate: WorkspaceWriteLockPort | None) -> None:
        self._delegate = delegate or NullWorkspaceWriteLock()

    async def acquire_workspace_write_lock(self, thread_id: str) -> bool:
        return await self._delegate.acquire_workspace_write_lock(thread_id)

    def release_workspace_write_lock(self, thread_id: str) -> None:
        self._delegate.release_workspace_write_lock(thread_id)
