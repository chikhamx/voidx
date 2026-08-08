from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

@dataclass
class FileStateStore:
    mtimes: dict[str, dict[str, int]] = field(default_factory=dict)
    read_coverage: dict[str, dict[str, object]] = field(default_factory=dict)


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
EDIT_COVERAGE_GAP_TOLERANCE = 3


@dataclass(frozen=True)
class LineDriftMap:
    epoch: int
    source_ranges: list[ReadLineRange]
    span_steps: list[list[DiffSpan]]


def line_drift_maps_from_raw(raw: list[dict] | None) -> list[LineDriftMap]:
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


def line_drift_maps_to_raw(maps: list[LineDriftMap]) -> list[dict]:
    raw: list[dict] = []
    for m in maps:
        raw.append({
            "epoch": m.epoch,
            "source_ranges": [asdict(r) for r in m.source_ranges],
            "span_steps": [[asdict(s) for s in step] for step in m.span_steps],
        })
    return raw


def file_fingerprint(resolved: Path) -> FileFingerprint:
    stat = resolved.stat()
    return FileFingerprint(mtime_ns=stat.st_mtime_ns, size=stat.st_size)
def diff_spans_from_file_diff(file_diff: FileDiff) -> list[DiffSpan]:
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
def remap_old_range(start: int, end: int, spans: list[DiffSpan]) -> list[dict]:
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


def line_numbers_to_ranges(line_numbers: list[int]) -> list[tuple[int, int]]:
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
