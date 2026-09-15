"""Run-owned OS locks; cooperating headless processes must share DATA_DIR."""
from __future__ import annotations

import asyncio
import os
from hashlib import sha256
from pathlib import Path

from voidx.persistence import sqlite as store
from voidx.persistence.file_lock import acquire_file_lock_sync, release_file_lock_sync


def normalize_workspace(workspace: str) -> str:
    return os.path.normcase(str(Path(workspace).resolve()))


class HeadlessFileLock:
    def __init__(self, kind: str, identity: str) -> None:
        digest = sha256(identity.encode()).hexdigest()
        self._path = store.DATA_DIR / "store" / "headless-locks" / f"{kind}-{digest}.lock"
        self._handle = None

    async def acquire(self) -> None:
        while True:
            try:
                self._handle = acquire_file_lock_sync(self._path, blocking=False)
                return
            except BlockingIOError:
                await asyncio.sleep(0.05)

    def release(self) -> None:
        if self._handle is not None:
            handle, self._handle = self._handle, None
            release_file_lock_sync(handle)
        # Never unlink: a waiter may already have opened the same inode.


class HeadlessWorkspaceWriteLock(HeadlessFileLock):
    def __init__(self, workspace: str) -> None:
        super().__init__("workspace", normalize_workspace(workspace))

    async def acquire_workspace_write_lock(self, thread_id: str) -> bool:
        await self.acquire()
        return True

    def release_workspace_write_lock(self, thread_id: str) -> None:
        self.release()


class OwnedWorkspaceWriteLock:
    """Run-scoped capability; create once per owner and normalized workspace.

    Pass child(child_id) to each execution's workspace_write_lock port. Registration
    is lazy: only the first write takes the OS lock. Child release returns the
    exclusive write borrow, while close releases the owner OS handle. The parent
    must not acquire a write borrow while awaiting children. Call close after
    stopping producers; it also revokes pending and previously issued capabilities.
    This object and its children are confined to one asyncio event loop.
    """

    def __init__(self, workspace: str) -> None:
        self._lock = HeadlessWorkspaceWriteLock(workspace)
        self._closed = False
        self._borrow: tuple[object, str, asyncio.Task | None] | None = None
        self._changed = asyncio.Event()

    def child(self, child_id: str) -> OwnedWorkspaceWriteLease:
        self._ensure_open()
        return OwnedWorkspaceWriteLease(self, child_id)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Workspace lock owner is closed")

    async def _acquire(self, child: object, thread_id: str) -> bool:
        while True:
            self._ensure_open()
            self._changed.clear()
            if self._borrow is not None:
                await self._changed.wait()
                continue
            if self._lock._handle is None:
                try:
                    # No suspension between taking the OS handle and recording
                    # the borrow: cancelling a waiter cannot orphan either.
                    self._lock._handle = acquire_file_lock_sync(self._lock._path, blocking=False)
                except BlockingIOError:
                    try:
                        await asyncio.wait_for(self._changed.wait(), 0.05)
                    except TimeoutError:
                        pass  # Poll the OS lock; close wakes this wait immediately.
                    continue
            self._borrow = (child, thread_id, asyncio.current_task())
            return True

    def _release(self, child: object, thread_id: str) -> None:
        if self._borrow == (child, thread_id, asyncio.current_task()):
            self._borrow = None
            self._changed.set()

    def close(self) -> None:
        """Idempotently revoke all borrows and release the owner handle last."""
        self._closed = True
        self._borrow = None
        self._changed.set()
        self._lock.release()


class OwnedWorkspaceWriteLease:
    """Per-child port facade; possessing a matching identity is not a capability."""

    def __init__(self, owner: OwnedWorkspaceWriteLock, child_id: str) -> None:
        self.child_id = child_id
        self._owner = owner

    async def acquire_workspace_write_lock(self, thread_id: str) -> bool:
        return await self._owner._acquire(self, thread_id)

    def release_workspace_write_lock(self, thread_id: str) -> None:
        self._owner._release(self, thread_id)
