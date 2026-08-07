from __future__ import annotations

import hashlib
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from voidx.tooling.domain.file_tracking import FileStateStore
from voidx.tooling.domain.diff import FileDiff
from voidx.tooling.domain.file_tracking import (
    EDIT_COVERAGE_GAP_TOLERANCE,
    MAX_LINE_DRIFT_MAPS_PER_FILE,
    LineDriftMap,
    ReadLineRange,
    _diff_spans_from_file_diff,
    _line_drift_maps_from_raw,
    _line_drift_maps_to_raw,
    _line_numbers_to_ranges,
    file_fingerprint,
    remap_old_range,
)


class FileStateContext(Protocol):
    file_state: FileStateStore


def _store(value: FileStateStore | FileStateContext) -> FileStateStore:
    return value if isinstance(value, FileStateStore) else value.file_state


def get_line_drift_maps(state: object, resolved: Path) -> list[LineDriftMap]:
    store = _store(state)
    coverage = store.read_coverage.get(str(resolved.resolve()))
    return [] if coverage is None else _line_drift_maps_from_raw(coverage.get("line_drift_maps"))


def check_staleness(state: object, resolved: Path) -> str | None:
    store = _store(state)
    key = str(resolved.resolve())
    if key not in store.mtimes:
        return None
    if not resolved.exists():
        return f"File deleted since last read: {resolved}"
    if asdict(file_fingerprint(resolved)) != store.mtimes[key]:
        return f"File was modified since last read: {resolved}. Please re-read the file before editing."
    return None


def record_mtime(state: object, resolved: Path) -> None:
    store = _store(state)
    if resolved.exists():
        store.mtimes[str(resolved.resolve())] = asdict(file_fingerprint(resolved))


def clear_read_coverage(state: object, resolved: Path) -> None:
    _store(state).read_coverage.pop(str(resolved.resolve()), None)


def clear_file_tracking(state: object, resolved: Path) -> None:
    store = _store(state)
    key = str(resolved.resolve())
    store.read_coverage.pop(key, None)
    store.mtimes.pop(key, None)


def clear_tree_tracking(state: object, root: Path) -> None:
    store = _store(state)
    resolved_root = root.resolve()
    for mapping in (store.mtimes, store.read_coverage):
        for key in list(mapping):
            if Path(key).is_relative_to(resolved_root):
                mapping.pop(key, None)


def move_file_tracking(state: object, source: Path, dest: Path) -> None:
    store = _store(state)
    source_key = str(source.resolve())
    dest_key = str(dest.resolve())
    coverage = store.read_coverage.pop(source_key, None)
    store.mtimes.pop(source_key, None)
    if coverage is not None and dest.exists():
        moved = coverage.copy()
        moved["fingerprint"] = asdict(file_fingerprint(dest))
        store.read_coverage[dest_key] = moved
    record_mtime(store, dest)


def _merge_ranges(ranges: list[dict]) -> list[dict]:
    if not ranges:
        return []
    merged = [item.copy() for item in sorted(ranges, key=lambda item: item["start_line"])]
    output = [merged[0]]
    for item in merged[1:]:
        last = output[-1]
        if item["start_line"] <= last["end_line"] + 1:
            last["end_line"] = max(last["end_line"], item["end_line"])
        else:
            output.append(item)
    return output


def record_read_range(state: object, resolved: Path, start_line: int, end_line: int) -> None:
    store = _store(state)
    if not resolved.exists() or end_line < start_line:
        return
    key = str(resolved.resolve())
    fingerprint = asdict(file_fingerprint(resolved))
    existing = store.read_coverage.get(key, {})
    fp_match = existing.get("fingerprint") == fingerprint
    ranges = existing.get("ranges", []) if fp_match else []
    existing_maps = _line_drift_maps_from_raw(existing.get("line_drift_maps")) if fp_match else []
    next_epoch = max((item.epoch for item in existing_maps), default=0) + 1 if fp_match else 1
    updated_maps = [*existing_maps, LineDriftMap(epoch=next_epoch, source_ranges=[ReadLineRange(start_line, end_line)], span_steps=[])]
    updated_maps = sorted(updated_maps, key=lambda item: item.epoch)[-MAX_LINE_DRIFT_MAPS_PER_FILE:]
    store.read_coverage[key] = {
        "fingerprint": fingerprint,
        "ranges": _merge_ranges([*ranges, asdict(ReadLineRange(start_line, end_line))]),
        "line_drift_maps": _line_drift_maps_to_raw(updated_maps),
    }
    record_mtime(store, resolved)


def remap_read_coverage_from_file_diff(state: object, resolved: Path, file_diff: FileDiff, *, old_ranges: list[dict]) -> None:
    store = _store(state)
    if not resolved.exists():
        clear_read_coverage(store, resolved)
        return
    spans = _diff_spans_from_file_diff(file_diff)
    remapped: list[dict] = []
    for item in old_ranges:
        start, end = int(item.get("start_line", 0)), int(item.get("end_line", 0))
        if start > 0 and end >= start:
            remapped.extend(remap_old_range(start, end, spans))
    visible_lines = [line.new_lineno for hunk in file_diff.hunks for line in hunk.lines if line.kind in {"add", "context"} and line.new_lineno is not None]
    visible_ranges = [asdict(ReadLineRange(start, end)) for start, end in _line_numbers_to_ranges(visible_lines)]
    ranges = _merge_ranges([*remapped, *visible_ranges])
    key = str(resolved.resolve())
    existing_maps = _line_drift_maps_from_raw(store.read_coverage.get(key, {}).get("line_drift_maps"))
    updated_maps = [LineDriftMap(epoch=item.epoch, source_ranges=item.source_ranges, span_steps=[*item.span_steps, spans]) for item in existing_maps]
    if ranges:
        store.read_coverage[key] = {"fingerprint": asdict(file_fingerprint(resolved)), "ranges": ranges, "line_drift_maps": _line_drift_maps_to_raw(updated_maps)}
    else:
        clear_read_coverage(store, resolved)
    record_mtime(store, resolved)


def coverage_ranges_snapshot(state: object, resolved: Path) -> list[dict]:
    store = _store(state)
    existing = store.read_coverage.get(str(resolved.resolve()), {})
    if existing.get("fingerprint") != asdict(file_fingerprint(resolved)):
        return []
    return [item.copy() for item in existing.get("ranges", [])]


def covered_read_range(state: object, resolved: Path, start_line: int, end_line: int, *, gap_tolerance: int = 0) -> tuple[int, int] | None:
    store = _store(state)
    coverage = store.read_coverage.get(str(resolved.resolve()))
    if not coverage or coverage.get("fingerprint") != asdict(file_fingerprint(resolved)):
        return None
    ranges = sorted(
        (
            (int(item["start_line"]), int(item["end_line"]))
            for item in coverage.get("ranges", [])
        ),
    )
    merged: list[tuple[int, int]] = []
    for item_start, item_end in ranges:
        if merged and item_start - merged[-1][1] - 1 <= gap_tolerance:
            merged[-1] = (merged[-1][0], max(merged[-1][1], item_end))
        else:
            merged.append((item_start, item_end))
    for covered_start, covered_end in merged:
        if covered_start <= start_line and covered_end >= end_line:
            return covered_start, covered_end
    return None


def check_read_coverage(state: object, resolved: Path, start_line: int, end_line: int, *, display_path: str | None = None) -> str | None:
    store = _store(state)
    shown = display_path or str(resolved)
    if covered_read_range(store, resolved, start_line, end_line, gap_tolerance=EDIT_COVERAGE_GAP_TOLERANCE) is not None:
        return None
    key = str(resolved.resolve())
    if key not in store.read_coverage:
        return f"File must be read before editing: {shown}. Please read the file first."
    return f"Lines {start_line}-{end_line} of {shown} must be read before editing. Please read that range first."


def content_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = ["LineDriftMap", "check_read_coverage", "check_staleness", "clear_file_tracking", "clear_read_coverage", "clear_tree_tracking", "content_digest", "coverage_ranges_snapshot", "covered_read_range", "get_line_drift_maps", "move_file_tracking", "record_mtime", "record_read_range", "remap_read_coverage_from_file_diff"]
