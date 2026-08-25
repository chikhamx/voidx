"""JSONL helpers for per-session storage.

Writes are serialized by per-session asyncio locks and OS-backed file locks so
multiple persistence processes cannot mutate one session directory concurrently.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import re
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import voidx.persistence.sqlite as store
from voidx.persistence.session_ids import validate_session_storage_id


_MAX_SESSION_LOCKS = 64
_CONTEXT_FRAME_FILE_RE = re.compile(r"^context/\d+\.jsonl$")
_session_locks: dict[str, asyncio.Lock] = {}
_locks_lock = asyncio.Lock()
_held_session_lock_ids: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar(
    "held_session_lock_ids", default=frozenset()
)


def session_dir(session_id: str) -> Path:
    safe_id = validate_session_storage_id(session_id)
    return store.DATA_DIR / "sessions" / safe_id


async def _get_lock(session_id: str) -> asyncio.Lock:
    async with _locks_lock:
        lock = _session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            _session_locks[session_id] = lock
            # Do not evict locks while callers may retain references or be queued on them.
            # Session ids are bounded by the process lifetime and explicit drop_session_lock.
        return lock


async def drop_session_lock(session_id: str) -> None:
    async with _locks_lock:
        lock = _session_locks.get(session_id)
        if lock is not None and not lock.locked():
            _session_locks.pop(session_id, None)


def _normalize_session_ids(session_ids: list[str] | tuple[str, ...]) -> list[str]:
    return sorted({validate_session_storage_id(session_id) for session_id in session_ids})


try:
    import fcntl
except ImportError:  # pragma: no cover - unavailable on Windows.
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - unavailable on POSIX.
    msvcrt = None


def _session_lock_path(session_id: str) -> Path:
    return session_dir(session_id).parent / f".{session_id}.lock"


def _acquire_file_lock_sync(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            return handle, "fcntl"
        if msvcrt is not None:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    return handle, "msvcrt"
                except OSError:
                    time.sleep(0.05)
        raise RuntimeError("cross-process session directory locking is unavailable")
    except Exception:
        handle.close()
        raise


def _release_file_lock_sync(locked_handle) -> None:
    handle, backend = locked_handle
    try:
        if backend == "fcntl":
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        elif backend == "msvcrt":
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        handle.close()


@asynccontextmanager
async def session_directory_locks(session_ids: list[str] | tuple[str, ...]):
    normalized = _normalize_session_ids(session_ids)
    held = _held_session_lock_ids.get()
    missing = [session_id for session_id in normalized if session_id not in held]
    locks = [await _get_lock(session_id) for session_id in missing]
    acquired: list[asyncio.Lock] = []
    file_handles = []
    token = _held_session_lock_ids.set(held | set(normalized))
    try:
        for lock in locks:
            await lock.acquire()
            acquired.append(lock)
        for session_id in missing:
            file_handles.append(
                await asyncio.to_thread(
                    _acquire_file_lock_sync, _session_lock_path(session_id)
                )
            )
        yield normalized
    finally:
        for handle in reversed(file_handles):
            await asyncio.to_thread(_release_file_lock_sync, handle)
        for lock in reversed(acquired):
            lock.release()
        _held_session_lock_ids.reset(token)


def _delete_session_directories_locked(session_ids: list[str]) -> None:
    for session_id in session_ids:
        path = session_dir(session_id)
        if path.exists():
            shutil.rmtree(path)


async def _delete_session_directories_locked_async(session_ids: list[str]) -> None:
    await asyncio.to_thread(_delete_session_directories_locked, session_ids)


def _append_jsonl_records_sync(path: Path, records: list[dict[str, Any]]) -> tuple[list[int], int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    offsets: list[int] = []
    with path.open("a+", encoding="utf-8") as f:
        f.seek(0, os.SEEK_END)
        for record in records:
            offsets.append(f.tell())
            f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        f.flush()
        os.fsync(f.fileno())
        return offsets, f.tell()


def _append_jsonl_sync(path: Path, record: dict[str, Any]) -> None:
    _append_jsonl_records_sync(path, [record])



def encode_jsonl_record(record: dict[str, Any]) -> bytes:
    """Encode one deterministic UTF-8 JSON object with exactly one trailing LF."""
    payload = json.dumps(
        record,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return payload + b"\n"


def _append_jsonl_bytes_sync(path: Path, payload: bytes) -> tuple[int, int]:
    if not payload.endswith(b"\n") or payload.endswith(b"\n\n"):
        raise ValueError("JSONL payload must end with exactly one LF")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab+") as handle:
        handle.seek(0, os.SEEK_END)
        start_offset = handle.tell()
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        return start_offset, handle.tell()


def _truncate_file_sync(path: Path, size: int) -> None:
    with path.open("r+b") as handle:
        handle.truncate(size)
        handle.flush()
        os.fsync(handle.fileno())


def _read_file_range_sync(path: Path, start_offset: int, end_offset: int) -> bytes | None:
    if start_offset < 0 or end_offset <= start_offset:
        return None
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        if end_offset > handle.tell():
            return None
        handle.seek(start_offset)
        payload = handle.read(end_offset - start_offset)
        if len(payload) != end_offset - start_offset:
            return None
        return payload


async def append_session_bytes(
    session_id: str,
    filename: str,
    payload: bytes,
) -> tuple[int, int]:
    """Append already encoded bytes and return the exact byte range."""
    async with session_directory_locks((session_id,)):
        return await asyncio.to_thread(
            _append_jsonl_bytes_sync,
            session_dir(session_id) / filename,
            payload,
        )


async def truncate_session_file(session_id: str, filename: str, size: int) -> None:
    """Durably truncate a session file while holding its directory lock."""
    if size < 0:
        raise ValueError("truncate size must not be negative")
    async with session_directory_locks((session_id,)):
        path = session_dir(session_id) / filename
        if path.exists():
            await asyncio.to_thread(_truncate_file_sync, path, size)


async def read_session_bytes(
    session_id: str,
    filename: str,
    start_offset: int,
    end_offset: int,
) -> bytes | None:
    """Read one exact byte range without JSONL best-effort recovery."""
    async with session_directory_locks((session_id,)):
        path = session_dir(session_id) / filename
        if not path.exists():
            return None
        return await asyncio.to_thread(
            _read_file_range_sync,
            path,
            start_offset,
            end_offset,
        )


def _write_jsonl_sync(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _write_json_sync(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)
    # Windows: os.open(dir) not supported, skip dir fsync
    if os.name != "nt":
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def _read_jsonl_sync(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def _read_jsonl_from_offset_sync(path: Path, offset: int) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("rb") as f:
        f.seek(max(offset, 0))
        for raw_line in f:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def _read_jsonl_between_offsets_sync(
    path: Path,
    start_offset: int,
    end_offset: int,
) -> list[dict[str, Any]] | None:
    records: list[dict[str, Any]] = []
    with path.open("rb") as f:
        f.seek(max(start_offset, 0))
        while f.tell() < end_offset:
            raw_line = f.readline()
            if not raw_line:
                return None
            raw_line = raw_line.strip()
            if not raw_line:
                return None
            try:
                line = raw_line.decode("utf-8")
                record = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            if not isinstance(record, dict):
                return None
            records.append(record)
    return records


async def read_session_records_between_offsets(
    session_id: str,
    filename: str,
    start_offset: int,
    end_offset: int,
) -> list[dict[str, Any]] | None:
    if not session_dir(session_id).exists():
        return None
    async with session_directory_locks((session_id,)):
        path = session_dir(session_id) / filename
        if not path.exists():
            return None
        return await asyncio.to_thread(
            _read_jsonl_between_offsets_sync,
            path,
            start_offset,
            end_offset,
        )


async def append_session_record(
    session_id: str,
    filename: str,
    record: dict[str, Any],
) -> int:
    """Append one record while holding the session's cross-process lock."""
    async with session_directory_locks((session_id,)):
        path = session_dir(session_id) / filename
        offsets, _ = await asyncio.to_thread(_append_jsonl_records_sync, path, [record])
        return offsets[0] if offsets else 0


async def append_session_records(
    session_id: str,
    filename: str,
    records: list[dict[str, Any]],
) -> tuple[list[int], int]:
    """Append records atomically while holding the session's cross-process lock."""
    async with session_directory_locks((session_id,)):
        path = session_dir(session_id) / filename
        return await asyncio.to_thread(_append_jsonl_records_sync, path, records)


def _replace_jsonl_records_sync(path: Path, records: list[dict[str, Any]]) -> tuple[list[int], int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    offsets: list[int] = []
    try:
        with temp_path.open("w", encoding="utf-8") as f:
            for record in records:
                offsets.append(f.tell())
                f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                f.write("\n")
            f.flush()
            os.fsync(f.fileno())
            size = f.tell()
        os.replace(temp_path, path)
        if os.name != "nt":
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        return offsets, size
    finally:
        if temp_path.exists():
            temp_path.unlink()


async def replace_session_records(
    session_id: str,
    filename: str,
    records: list[dict[str, Any]],
) -> tuple[list[int], int]:
    """Atomically replace one session JSONL file under the session lock."""
    async with session_directory_locks((session_id,)):
        path = session_dir(session_id) / filename
        return await asyncio.to_thread(_replace_jsonl_records_sync, path, records)


async def write_session_records(
    session_id: str,
    filename: str,
    records: list[dict[str, Any]],
) -> None:
    async with session_directory_locks((session_id,)):
        path = session_dir(session_id) / filename
        await asyncio.to_thread(_write_jsonl_sync, path, records)


async def write_session_json(
    session_id: str,
    filename: str,
    value: dict[str, Any],
) -> None:
    async with session_directory_locks((session_id,)):
        path = session_dir(session_id) / filename
        await asyncio.to_thread(_write_json_sync, path, value)


async def read_session_records(
    session_id: str,
    filename: str,
) -> list[dict[str, Any]] | None:
    if not session_dir(session_id).exists():
        return None
    async with session_directory_locks((session_id,)):
        path = session_dir(session_id) / filename
        if not path.exists():
            return None
        return await asyncio.to_thread(_read_jsonl_sync, path)


async def read_session_records_from_offset(
    session_id: str,
    filename: str,
    offset: int,
) -> list[dict[str, Any]] | None:
    if not session_dir(session_id).exists():
        return None
    async with session_directory_locks((session_id,)):
        path = session_dir(session_id) / filename
        if not path.exists():
            return None
        return await asyncio.to_thread(_read_jsonl_from_offset_sync, path, offset)


def _unlink_session_file_sync(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


async def delete_session_file(session_id: str, filename: str) -> bool:
    if not _CONTEXT_FRAME_FILE_RE.fullmatch(filename):
        raise ValueError(f"refusing to delete session file: {filename}")
    async with session_directory_locks((session_id,)):
        path = session_dir(session_id) / filename
        return await asyncio.to_thread(_unlink_session_file_sync, path)


async def delete_session_directories(session_ids: list[str] | tuple[str, ...]) -> None:
    normalized = _normalize_session_ids(session_ids)
    async with session_directory_locks(normalized):
        await _delete_session_directories_locked_async(normalized)
