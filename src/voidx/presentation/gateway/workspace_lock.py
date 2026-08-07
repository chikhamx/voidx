"""Narrow workspace-write lock adapter for gateway runs."""

from __future__ import annotations

from voidx.agent.ports.workspace_lock import WorkspaceWriteLockPort


class GatewayWorkspaceWriteLock:
    def __init__(self, delegate: WorkspaceWriteLockPort) -> None:
        self._delegate = delegate

    async def acquire_workspace_write_lock(self, thread_id: str) -> bool:
        return await self._delegate.acquire_workspace_write_lock(thread_id)

    def release_workspace_write_lock(self, thread_id: str) -> None:
        self._delegate.release_workspace_write_lock(thread_id)
