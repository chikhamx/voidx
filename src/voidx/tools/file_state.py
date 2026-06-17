"""Shared file state helpers for write-like tools."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from voidx.memory.jsonl_store import session_dir
from voidx.tools.base import ToolContext


@dataclass(frozen=True)
class FileFingerprint:
    mtime_ns: int
    size: int


@dataclass(frozen=True)
class ReadLineRange:
    start_line: int
    end_line: int


def check_staleness(ctx: ToolContext, resolved: Path) -> str | None:
    key = str(resolved.resolve())
    if key not in ctx.file_mtimes:
        return None
    if not resolved.exists():
        return f"File deleted since last read: {resolved}"
    current_fingerprint = asdict(file_fingerprint(resolved))
    if current_fingerprint != ctx.file_mtimes[key]:
        return (
            f"File was modified since last read: {resolved}. "
            "Please re-read the file before editing."
        )
    return None


def record_mtime(ctx: ToolContext, resolved: Path) -> None:
    if resolved.exists():
        ctx.file_mtimes[str(resolved.resolve())] = asdict(file_fingerprint(resolved))


def clear_read_coverage(ctx: ToolContext, resolved: Path) -> None:
    ctx.file_read_coverage.pop(str(resolved.resolve()), None)


def _merge_ranges(ranges: list[dict]) -> list[dict]:
    if not ranges:
        return []
    sorted_ranges = sorted(ranges, key=lambda r: r["start_line"])
    merged = [sorted_ranges[0].copy()]
    for r in sorted_ranges[1:]:
        last = merged[-1]
        if r["start_line"] <= last["end_line"] + 1:
            last["end_line"] = max(last["end_line"], r["end_line"])
        else:
            merged.append(r.copy())
    return merged


def record_read_range(ctx: ToolContext, resolved: Path, start_line: int, end_line: int) -> None:
    if not resolved.exists() or end_line < start_line:
        return
    key = str(resolved.resolve())
    fingerprint = asdict(file_fingerprint(resolved))
    existing = ctx.file_read_coverage.get(key, {})
    ranges = existing.get("ranges", []) if existing.get("fingerprint") == fingerprint else []
    ctx.file_read_coverage[key] = {
        "fingerprint": fingerprint,
        "ranges": _merge_ranges([*ranges, asdict(ReadLineRange(start_line, end_line))]),
    }
    record_mtime(ctx, resolved)


def check_read_coverage(ctx: ToolContext, resolved: Path, start_line: int, end_line: int) -> str | None:
    if covered_read_range(ctx, resolved, start_line, end_line) is not None:
        return None
    key = str(resolved.resolve())
    coverage = ctx.file_read_coverage.get(key)
    if coverage is None:
        return f"Lines {start_line}-{end_line} in {resolved} must be read before editing."
    if coverage.get("fingerprint") != asdict(file_fingerprint(resolved)):
        return (
            f"File was modified since last read: {resolved}. "
            "Please re-read the file before editing."
        )
    return f"Lines {start_line}-{end_line} in {resolved} must be read before editing."


def covered_read_range(ctx: ToolContext, resolved: Path, start_line: int, end_line: int) -> ReadLineRange | None:
    key = str(resolved.resolve())
    coverage = ctx.file_read_coverage.get(key)
    if coverage is None:
        return None
    if coverage.get("fingerprint") != asdict(file_fingerprint(resolved)):
        return None
    ranges = coverage.get("ranges", [])
    for item in ranges:
        if item.get("start_line") <= start_line and end_line <= item.get("end_line"):
            return ReadLineRange(int(item.get("start_line")), int(item.get("end_line")))
    return None


def file_fingerprint(resolved: Path) -> FileFingerprint:
    stat = resolved.stat()
    return FileFingerprint(mtime_ns=stat.st_mtime_ns, size=stat.st_size)


async def save_file_version(
    ctx: ToolContext,
    resolved: Path,
    *,
    display_path: str | None = None,
    tool_name: str = "",
) -> None:
    """Save a pre-modification snapshot for session-scoped file history."""
    if not resolved.exists() or not resolved.is_file():
        return

    history_dir = session_dir(ctx.session_id) / "file-history"
    manifest_path = history_dir / "manifest.jsonl"

    full_hash = hashlib.sha256(str(resolved.resolve()).encode("utf-8")).hexdigest()
    short_hash = full_hash[:16]
    existing_rows = await asyncio.to_thread(_read_manifest_rows, manifest_path)
    version = _next_version(existing_rows, full_hash)
    snapshot_name = _snapshot_name(existing_rows, full_hash, short_hash, version)
    snapshot_path = history_dir / snapshot_name

    file_bytes = await asyncio.to_thread(resolved.read_bytes)
    await asyncio.to_thread(_write_snapshot, snapshot_path, file_bytes)
    await asyncio.to_thread(_append_manifest_row, manifest_path, {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "path": display_path or _display_path(ctx.workspace, resolved),
        "resolved_path": str(resolved.resolve()),
        "full_hash": full_hash,
        "short_hash": short_hash,
        "version": version,
        "snapshot": snapshot_name,
        "tool": tool_name,
    })


def _read_manifest_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _next_version(rows: list[dict], full_hash: str) -> int:
    versions = [
        row.get("version")
        for row in rows
        if row.get("full_hash") == full_hash and isinstance(row.get("version"), int)
    ]
    return max(versions, default=0) + 1


def _snapshot_name(rows: list[dict], full_hash: str, short_hash: str, version: int) -> str:
    has_short_collision = any(
        row.get("short_hash") == short_hash and row.get("full_hash") != full_hash
        for row in rows
    )
    prefix = full_hash if has_short_collision else short_hash
    return f"{prefix}@v{version}"


def _write_snapshot(path: Path, content: bytes) -> None:
    tmp_path = path.with_name(f"{path.name}.tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tmp_path.open("wb") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)
    _fsync_dir(path.parent)


def _append_manifest_row(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return  # Windows does not support opening directories as file descriptors
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _display_path(workspace: str, resolved: Path) -> str:
    try:
        return str(resolved.relative_to(workspace))
    except ValueError:
        return str(resolved)
