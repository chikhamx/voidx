"""Shared synchronous OS-backed file locks."""
from __future__ import annotations

import errno
import os
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - unavailable on Windows.
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - unavailable on POSIX.
    msvcrt = None


def acquire_file_lock_sync(
    path: Path, *, blocking: bool = True, fcntl_backend=fcntl, msvcrt_backend=msvcrt
):
    """Acquire an OS lock; nonblocking contention raises BlockingIOError.

    Backend overrides support callers retaining legacy platform capability hooks.
    The returned handle must be released with release_file_lock_sync.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if fcntl_backend is not None:
            fcntl_backend.flock(handle.fileno(), fcntl_backend.LOCK_EX | (0 if blocking else fcntl_backend.LOCK_NB))
            return handle, "fcntl"
        if msvcrt_backend is not None:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            while True:
                try:
                    msvcrt_backend.locking(handle.fileno(), msvcrt_backend.LK_LOCK if blocking else msvcrt_backend.LK_NBLCK, 1)
                    return handle, "msvcrt"
                except OSError as error:
                    if not blocking:
                        if error.errno == errno.EACCES:
                            raise BlockingIOError(errno.EAGAIN, "file lock is busy") from error
                        raise
                    time.sleep(0.05)
        raise RuntimeError("cross-process session directory locking is unavailable")
    except Exception:
        handle.close()
        raise


def release_file_lock_sync(
    locked_handle, *, fcntl_backend=fcntl, msvcrt_backend=msvcrt
) -> None:
    """Unlock and close a handle without unlinking its shared lock file."""
    handle, backend = locked_handle
    try:
        if backend == "fcntl":
            fcntl_backend.flock(handle.fileno(), fcntl_backend.LOCK_UN)
        elif backend == "msvcrt":
            handle.seek(0)
            msvcrt_backend.locking(handle.fileno(), msvcrt_backend.LK_UNLCK, 1)
    finally:
        handle.close()


