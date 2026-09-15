from __future__ import annotations

import pytest

from voidx.persistence import jsonl


def test_public_file_lock_shares_jsonl_lock(tmp_path):
    from voidx.persistence.file_lock import acquire_file_lock_sync, release_file_lock_sync

    path = tmp_path / "nested" / "shared.lock"
    locked = jsonl._acquire_file_lock_sync(path)
    try:
        with pytest.raises(BlockingIOError):
            acquire_file_lock_sync(path, blocking=False)
    finally:
        jsonl._release_file_lock_sync(locked)
    assert locked[0].closed

    locked = acquire_file_lock_sync(path, blocking=False)
    try:
        with pytest.raises(BlockingIOError):
            jsonl._acquire_file_lock_sync(path, blocking=False)
    finally:
        release_file_lock_sync(locked)
    assert locked[0].closed
    assert path.exists()


def test_public_file_lock_fails_closed_without_backends(tmp_path):
    from voidx.persistence.file_lock import acquire_file_lock_sync

    with pytest.raises(RuntimeError, match="locking is unavailable"):
        acquire_file_lock_sync(
            tmp_path / "shared.lock", fcntl_backend=None, msvcrt_backend=None
        )
