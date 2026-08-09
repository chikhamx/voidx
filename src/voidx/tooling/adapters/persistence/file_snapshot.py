from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import uuid
import weakref
from datetime import datetime, timezone
from pathlib import Path

from voidx.platform.paths import voidx_workspace_dir
from voidx.tooling.domain.context import ToolExecutionContext as ToolContext


_SESSION_LOCKS: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
_SESSION_LOCKS_GUARD = threading.Lock()


def _lock_for_session(session_id: str) -> asyncio.Lock:
    with _SESSION_LOCKS_GUARD:
        lock = _SESSION_LOCKS.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            _SESSION_LOCKS[session_id] = lock
        return lock


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

    history_dir = voidx_workspace_dir(ctx.workspace) / "sessions" / ctx.session_id / "file-history"
    manifest_path = history_dir / "manifest.jsonl"
    resolved_path = resolved.resolve()
    full_hash = hashlib.sha256(str(resolved_path).encode("utf-8")).hexdigest()
    short_hash = full_hash[:16]

    async with _lock_for_session(ctx.session_id):
        transaction = asyncio.create_task(asyncio.to_thread(
            _save_file_version_transaction,
            manifest_path,
            history_dir,
            resolved,
            resolved_path,
            full_hash,
            short_hash,
            display_path or _display_path(ctx.workspace, resolved),
            tool_name,
        ))
        cancelled: asyncio.CancelledError | None = None
        while not transaction.done():
            try:
                await asyncio.shield(transaction)
            except asyncio.CancelledError as exc:
                cancelled = cancelled or exc
        transaction.result()
        if cancelled is not None:
            raise cancelled


def _save_file_version_transaction(
    manifest_path: Path,
    history_dir: Path,
    resolved: Path,
    resolved_path: Path,
    full_hash: str,
    short_hash: str,
    display_path: str,
    tool_name: str,
) -> None:
    existing_rows = _read_manifest_rows(manifest_path)
    version = _next_version(existing_rows, full_hash)
    snapshot_name = _snapshot_name(existing_rows, full_hash, short_hash, version)
    snapshot_path = history_dir / snapshot_name
    _write_snapshot(snapshot_path, resolved.read_bytes())
    _append_manifest_row(manifest_path, {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "path": display_path,
        "resolved_path": str(resolved_path),
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
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tmp_path.open("wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        _fsync_dir(path.parent)
    finally:
        tmp_path.unlink(missing_ok=True)


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
