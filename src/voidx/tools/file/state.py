"""Shared file state helpers for write-like tools."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from voidx.diffing import FileDiff
from voidx.memory import store
from voidx.memory.jsonl_store import session_dir
from voidx.paths import voidx_workspace_dir
from voidx.tools.base import ToolContext


@dataclass(frozen=True)
class FileFingerprint:
    mtime_ns: int
    size: int


@dataclass(frozen=True)
class ReadLineRange:
    start_line: int
    end_line: int


@dataclass(frozen=True)
class DiffSpan:
    old_start: int
    old_end: int
    offset: int


MAX_LINE_DRIFT_MAPS_PER_FILE = 16

# Edit coverage may span read ranges with at most this many unread lines between them.
EDIT_COVERAGE_GAP_TOLERANCE = 3


@dataclass(frozen=True)
class LineDriftMap:
    epoch: int
    source_ranges: list[ReadLineRange]
    span_steps: list[list[DiffSpan]]


def _line_drift_maps_from_raw(raw: list[dict] | None) -> list[LineDriftMap]:
    if not raw:
        return []
    maps: list[LineDriftMap] = []
    for item in raw:
        source_ranges = [
            ReadLineRange(int(r["start_line"]), int(r["end_line"]))
            for r in item.get("source_ranges", [])
        ]
        span_steps = [
            [DiffSpan(int(s["old_start"]), int(s["old_end"]), int(s["offset"])) for s in step]
            for step in item.get("span_steps", [])
        ]
        maps.append(LineDriftMap(
            epoch=int(item["epoch"]),
            source_ranges=source_ranges,
            span_steps=span_steps,
        ))
    return maps


def _line_drift_maps_to_raw(maps: list[LineDriftMap]) -> list[dict]:
    raw: list[dict] = []
    for m in maps:
        raw.append({
            "epoch": m.epoch,
            "source_ranges": [asdict(r) for r in m.source_ranges],
            "span_steps": [[asdict(s) for s in step] for step in m.span_steps],
        })
    return raw


def get_line_drift_maps(ctx: ToolContext, resolved: Path) -> list[LineDriftMap]:
    key = str(resolved.resolve())
    coverage = ctx.file_read_coverage.get(key)
    if coverage is None:
        return []
    return _line_drift_maps_from_raw(coverage.get("line_drift_maps"))


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


def clear_file_tracking(ctx: ToolContext, resolved: Path) -> None:
    key = str(resolved.resolve())
    ctx.file_read_coverage.pop(key, None)
    ctx.file_mtimes.pop(key, None)


def clear_tree_tracking(ctx: ToolContext, root: Path) -> None:
    resolved_root = root.resolve()
    for mapping in (ctx.file_mtimes, ctx.file_read_coverage):
        for key in list(mapping):
            if Path(key).is_relative_to(resolved_root):
                mapping.pop(key, None)


def move_file_tracking(ctx: ToolContext, source: Path, dest: Path) -> None:
    source_key = str(source.resolve())
    dest_key = str(dest.resolve())
    coverage = ctx.file_read_coverage.pop(source_key, None)
    ctx.file_mtimes.pop(source_key, None)
    if coverage is not None and dest.exists():
        moved = coverage.copy()
        moved["fingerprint"] = asdict(file_fingerprint(dest))
        ctx.file_read_coverage[dest_key] = moved
    record_mtime(ctx, dest)


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
    fp_match = existing.get("fingerprint") == fingerprint
    ranges = existing.get("ranges", []) if fp_match else []
    existing_maps = _line_drift_maps_from_raw(existing.get("line_drift_maps")) if fp_match else []
    next_epoch = (max((m.epoch for m in existing_maps), default=0) + 1) if fp_match else 1
    new_map = LineDriftMap(
        epoch=next_epoch,
        source_ranges=[ReadLineRange(start_line, end_line)],
        span_steps=[],
    )
    updated_maps = [*existing_maps, new_map]
    if len(updated_maps) > MAX_LINE_DRIFT_MAPS_PER_FILE:
        updated_maps = sorted(updated_maps, key=lambda m: m.epoch)[-MAX_LINE_DRIFT_MAPS_PER_FILE:]
    ctx.file_read_coverage[key] = {
        "fingerprint": fingerprint,
        "ranges": _merge_ranges([*ranges, asdict(ReadLineRange(start_line, end_line))]),
        "line_drift_maps": _line_drift_maps_to_raw(updated_maps),
    }
    record_mtime(ctx, resolved)


def _diff_spans_from_file_diff(file_diff: FileDiff) -> list[DiffSpan]:
    """Extract precise DiffSpans covering only removed/replaced lines.

    Unlike using hunk.old_start + hunk.old_count (which includes context
    lines), this walks each hunk's lines and groups consecutive 'remove'
    lines into spans.  The offset for each span is (new_count - old_count)
    computed from the add/remove lines within that span's neighborhood.
    """
    spans: list[DiffSpan] = []
    for hunk in sorted(file_diff.hunks, key=lambda item: item.old_start):
        # Group consecutive remove lines into segments
        segments: list[tuple[int, int]] = []
        seg_start: int | None = None
        seg_end: int | None = None
        for line in hunk.lines:
            if line.kind == "remove" and line.old_lineno is not None:
                if seg_start is None:
                    seg_start = line.old_lineno
                seg_end = line.old_lineno
            else:
                if seg_start is not None:
                    segments.append((seg_start, seg_end))
                    seg_start = seg_end = None
        if seg_start is not None:
            segments.append((seg_start, seg_end))

        hunk_offset = hunk.new_count - hunk.old_count
        if segments:
            for seg_s, seg_e in segments:
                spans.append(DiffSpan(old_start=seg_s, old_end=seg_e, offset=hunk_offset))
        elif hunk_offset != 0:
            # Pure insert (no removed lines): use a zero-width span at the
            # insertion point so _remap_old_range shifts subsequent lines.
            spans.append(DiffSpan(
                old_start=hunk.old_start,
                old_end=hunk.old_start - 1,
                offset=hunk_offset,
            ))
    return spans


def remap_read_coverage_from_file_diff(
    ctx: ToolContext,
    resolved: Path,
    file_diff: FileDiff,
    *,
    old_ranges: list[dict],
) -> None:
    if not resolved.exists():
        clear_read_coverage(ctx, resolved)
        return

    remapped: list[dict] = []
    spans = _diff_spans_from_file_diff(file_diff)
    for item in old_ranges:
        start = int(item.get("start_line", 0))
        end = int(item.get("end_line", 0))
        if start > 0 and end >= start:
            remapped.extend(_remap_old_range(start, end, spans))

    visible_lines = [
        line.new_lineno
        for hunk in file_diff.hunks
        for line in hunk.lines
        if line.kind in {"add", "context"} and line.new_lineno is not None
    ]
    visible_ranges = [
        asdict(ReadLineRange(start, end))
        for start, end in _line_numbers_to_ranges(visible_lines)
    ]

    ranges = _merge_ranges([*remapped, *visible_ranges])
    key = str(resolved.resolve())
    existing_maps = _line_drift_maps_from_raw(ctx.file_read_coverage.get(key, {}).get("line_drift_maps"))
    updated_maps = [
        LineDriftMap(
            epoch=m.epoch,
            source_ranges=m.source_ranges,
            span_steps=[*m.span_steps, spans],
        )
        for m in existing_maps
    ]
    if ranges:
        ctx.file_read_coverage[key] = {
            "fingerprint": asdict(file_fingerprint(resolved)),
            "ranges": ranges,
            "line_drift_maps": _line_drift_maps_to_raw(updated_maps),
        }
    else:
        clear_read_coverage(ctx, resolved)
    record_mtime(ctx, resolved)


def _remap_old_range(start: int, end: int, spans: list[DiffSpan]) -> list[dict]:
    ranges: list[dict] = []
    cursor = start
    cumulative_offset = 0

    for span in spans:
        if cursor > end:
            break
        if span.old_end < span.old_start:
            if span.old_start == 0:
                cumulative_offset += span.offset
                continue
            if cursor <= min(end, span.old_start):
                keep_end = min(end, span.old_start)
                ranges.append(asdict(ReadLineRange(
                    cursor + cumulative_offset,
                    keep_end + cumulative_offset,
                )))
                cursor = keep_end + 1
            cumulative_offset += span.offset
            continue
        if end < span.old_start:
            ranges.append(asdict(ReadLineRange(
                cursor + cumulative_offset,
                end + cumulative_offset,
            )))
            return ranges
        if cursor < span.old_start:
            keep_end = min(end, span.old_start - 1)
            ranges.append(asdict(ReadLineRange(
                cursor + cumulative_offset,
                keep_end + cumulative_offset,
            )))
            cursor = keep_end + 1
        if cursor <= span.old_end:
            cursor = span.old_end + 1
        cumulative_offset += span.offset

    if cursor <= end:
        ranges.append(asdict(ReadLineRange(
            cursor + cumulative_offset,
            end + cumulative_offset,
        )))
    return [item for item in ranges if item["start_line"] > 0 and item["end_line"] >= item["start_line"]]


def _line_numbers_to_ranges(line_numbers: list[int]) -> list[tuple[int, int]]:
    values = sorted(set(line_numbers))
    if not values:
        return []
    ranges: list[tuple[int, int]] = []
    start = prev = values[0]
    for value in values[1:]:
        if value == prev + 1:
            prev = value
            continue
        ranges.append((start, prev))
        start = prev = value
    ranges.append((start, prev))
    return ranges


def coverage_ranges_snapshot(ctx: ToolContext, resolved: Path) -> list[dict]:
    """Copy the stored read ranges when they still match the file on disk."""
    existing = ctx.file_read_coverage.get(str(resolved.resolve()), {})
    if existing.get("fingerprint") != asdict(file_fingerprint(resolved)):
        return []
    return [item.copy() for item in existing.get("ranges", [])]


def check_read_coverage(
    ctx: ToolContext,
    resolved: Path,
    start_line: int,
    end_line: int,
    *,
    display_path: str | None = None,
) -> str | None:
    """Return None when the range is covered, otherwise return an edit-blocking message."""
    shown = display_path or str(resolved)
    if covered_read_range(
        ctx, resolved, start_line, end_line, gap_tolerance=EDIT_COVERAGE_GAP_TOLERANCE
    ) is not None:
        return None
    key = str(resolved.resolve())
    coverage = ctx.file_read_coverage.get(key)
    if coverage is None:
        return f"Lines {start_line}-{end_line} in {shown} must be read before editing."
    if coverage.get("fingerprint") != asdict(file_fingerprint(resolved)):
        return (
            f"File was modified since last read: {shown}. "
            "Please re-read the file before editing."
        )
    return f"Lines {start_line}-{end_line} in {shown} must be read before editing."


def covered_read_range(
    ctx: ToolContext,
    resolved: Path,
    start_line: int,
    end_line: int,
    *,
    gap_tolerance: int = 0,
) -> ReadLineRange | None:
    key = str(resolved.resolve())
    coverage = ctx.file_read_coverage.get(key)
    if coverage is None:
        return None
    if coverage.get("fingerprint") != asdict(file_fingerprint(resolved)):
        return None
    overlapping = [
        item for item in coverage.get("ranges", [])
        if int(item.get("start_line", 0)) <= end_line and start_line <= int(item.get("end_line", 0))
    ]
    if not overlapping:
        return None
    first, last = overlapping[0], overlapping[-1]
    if int(first["start_line"]) > start_line or int(last["end_line"]) < end_line:
        return None
    covered_end = int(first["end_line"])
    for item in overlapping[1:]:
        if int(item["start_line"]) - covered_end - 1 > gap_tolerance:
            return None
        covered_end = max(covered_end, int(item["end_line"]))
    return ReadLineRange(int(first["start_line"]), int(last["end_line"]))


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


def persist_named_tool_result(
    content: str,
    name: str,
    *,
    session_id: str = "default",
    workspace: str | None = None,
) -> str:
    return _persist_to_disk(content, name, session_id=session_id, workspace=workspace)


def _persist_to_disk(
    content: str,
    tool_use_id: str,
    *,
    session_id: str = "default",
    workspace: str | None = None,
) -> str:
    safe_id = "".join(c for c in tool_use_id if c.isalnum() or c in "-_")
    dir_path = _tool_results_root(workspace) / session_id
    dir_path.mkdir(parents=True, exist_ok=True)
    file_path = dir_path / f"{safe_id}.txt"
    file_path.write_text(content, encoding="utf-8", errors="replace")
    return str(file_path)


def _tool_results_root(workspace: str | None = None) -> Path:
    if workspace:
        try:
            return voidx_workspace_dir(workspace) / "tool-results"
        except OSError:
            pass
    return store.DATA_DIR / "tool-results"
