"""JSONL append helpers for per-session storage."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import voidx.memory.store as store


_MAX_SESSION_LOCKS = 64
_session_locks: dict[str, asyncio.Lock] = {}
_locks_lock = asyncio.Lock()


def session_dir(session_id: str) -> Path:
    return store.DATA_DIR / "sessions" / session_id


async def _get_lock(session_id: str) -> asyncio.Lock:
    async with _locks_lock:
        lock = _session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            _session_locks[session_id] = lock
            if len(_session_locks) > _MAX_SESSION_LOCKS:
                for sid in [k for k, v in _session_locks.items() if not v.locked()]:
                    del _session_locks[sid]
                    if len(_session_locks) <= _MAX_SESSION_LOCKS:
                        break
        return lock


async def drop_session_lock(session_id: str) -> None:
    async with _locks_lock:
        lock = _session_locks.get(session_id)
        if lock is not None and not lock.locked():
            _session_locks.pop(session_id, None)


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


async def append_session_record(
    session_id: str,
    filename: str,
    record: dict[str, Any],
) -> int:
    lock = await _get_lock(session_id)
    path = session_dir(session_id) / filename
    async with lock:
        offsets, _ = await asyncio.to_thread(_append_jsonl_records_sync, path, [record])
        return offsets[0] if offsets else 0


async def append_session_records(
    session_id: str,
    filename: str,
    records: list[dict[str, Any]],
) -> tuple[list[int], int]:
    lock = await _get_lock(session_id)
    path = session_dir(session_id) / filename
    async with lock:
        return await asyncio.to_thread(_append_jsonl_records_sync, path, records)


async def write_session_records(
    session_id: str,
    filename: str,
    records: list[dict[str, Any]],
) -> None:
    lock = await _get_lock(session_id)
    path = session_dir(session_id) / filename
    async with lock:
        await asyncio.to_thread(_write_jsonl_sync, path, records)


async def write_session_json(
    session_id: str,
    filename: str,
    value: dict[str, Any],
) -> None:
    lock = await _get_lock(session_id)
    path = session_dir(session_id) / filename
    async with lock:
        await asyncio.to_thread(_write_json_sync, path, value)


async def read_session_records(
    session_id: str,
    filename: str,
) -> list[dict[str, Any]] | None:
    path = session_dir(session_id) / filename
    if not path.exists():
        return None
    return await asyncio.to_thread(_read_jsonl_sync, path)


async def read_session_records_from_offset(
    session_id: str,
    filename: str,
    offset: int,
) -> list[dict[str, Any]] | None:
    path = session_dir(session_id) / filename
    if not path.exists():
        return None
    return await asyncio.to_thread(_read_jsonl_from_offset_sync, path, offset)
