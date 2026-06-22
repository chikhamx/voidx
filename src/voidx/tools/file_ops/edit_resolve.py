from __future__ import annotations

from bisect import bisect_right
from typing import Literal

from .types import (
    TEXT_REPLACE_LINE_RADIUS,
    TEXT_REPLACE_SPAN_TOLERANCE,
    EditEntry,
    ParagraphResolution,
    ResolvedEdit,
)


def _validate_resolved_edits(edits: list[ResolvedEdit], total_lines: int) -> str | None:
    replacements: list[tuple[int, int]] = []
    insertion_locations: set[int] = set()
    for i, edit in enumerate(edits):
        if edit.operation == "replace":
            if (edit.start_line, edit.end_line) == (0, 0):
                insertion_locations.add(0)
                continue
            if edit.start_line < 1 or edit.end_line > total_lines:
                return f"Edit {i}: line number out of range for file with {total_lines} lines."
            if edit.end_line < edit.start_line:
                return f"Edit {i}: start_line must be <= end_line."
            replacements.append((edit.start_line, edit.end_line))
        else:
            if edit.start_line < 0 or edit.start_line > total_lines:
                return f"Edit {i}: line number out of range for file with {total_lines} lines."
            if edit.start_line in insertion_locations:
                return f"Edit {i}: multiple insertions at the same resolved location are ambiguous."
            insertion_locations.add(edit.start_line)

    for i, (start, end) in enumerate(replacements):
        for other_start, other_end in replacements[i + 1:]:
            if start <= other_end and other_start <= end:
                return "Edit ranges must not overlap."
        for location in insertion_locations:
            if start <= location <= end:
                return "Insert location must not be inside a replacement range."
    return None


def _find_text_segment(
    lines: list[str],
    start_no: int,
    end_no: int,
    prefix: str,
    suffix: str,
) -> tuple[int, int, int, int] | str:
    if start_no == end_no:
        return _find_single_line_segment(lines, start_no, prefix, suffix)

    prefix_lines = _find_line_candidates(lines, start_no, prefix)
    if not prefix_lines:
        prefix_target = "empty line" if prefix == "" else f"prefix {prefix!r}"
        return (
            f"{prefix_target} not found within ±{TEXT_REPLACE_LINE_RADIUS} "
            f"lines of start_no {start_no}. Read the file to get current content."
        )

    suffix_lines = _find_line_candidates(lines, end_no, suffix)
    if not suffix_lines:
        suffix_target = "empty line" if suffix == "" else f"suffix {suffix!r}"
        return (
            f"{suffix_target} not found within ±{TEXT_REPLACE_LINE_RADIUS} "
            f"lines of end_no {end_no}. Read the file to get current content."
        )

    ranked = _rank_line_range_pairs(prefix_lines, suffix_lines, start_no, end_no)
    if not ranked:
        return (
            "no valid replace range found: candidate ranges did not match "
            f"expected span {end_no - start_no} with less than "
            f"{TEXT_REPLACE_SPAN_TOLERANCE} lines of drift. "
            f"prefix candidates: {_format_lines(prefix_lines)}; "
            f"suffix candidates: {_format_lines(suffix_lines)}."
        )

    best_score = ranked[0][0]
    best = [item for item in ranked if item[0] == best_score]
    if len(best) > 1:
        ranges = ", ".join(f"{start}-{end}" for _, start, end in best)
        return (
            f"replace range is ambiguous: candidate ranges {ranges} have the same score. "
            "Provide more specific prefix/suffix or adjust start_no/end_no."
        )

    _, start_line, end_line = ranked[0]
    start_offset = _global_offset_for_line(lines, start_line)
    end_offset = _global_offset_for_line(lines, end_line) + len(lines[end_line - 1])
    return (
        start_offset,
        end_offset,
        start_line,
        end_line,
    )


def _find_single_line_segment(
    lines: list[str],
    target_line: int,
    prefix: str,
    suffix: str,
) -> tuple[int, int, int, int] | str:
    prefix_lines = _find_line_candidates(lines, target_line, prefix)
    if not prefix_lines:
        prefix_target = "empty line" if prefix == "" else f"prefix {prefix!r}"
        return (
            f"{prefix_target} not found within ±{TEXT_REPLACE_LINE_RADIUS} "
            f"lines of line {target_line}. Read the file to get current content."
        )

    if suffix != "" and suffix != prefix:
        prefix_lines = [
            line_no
            for line_no in prefix_lines
            if _line_matches_replace_anchor(lines[line_no - 1], suffix)
        ]
        if not prefix_lines:
            return (
                f"prefix {prefix!r} found but suffix {suffix!r} not on the same line "
                f"within ±{TEXT_REPLACE_LINE_RADIUS} lines of line {target_line}. "
                "Read the file to get current content."
            )

    prefix_lines.sort(key=lambda l: abs(l - target_line))
    best_dist = abs(prefix_lines[0] - target_line)
    best = [l for l in prefix_lines if abs(l - target_line) == best_dist]

    if len(best) > 1:
        return (
            f"single-line match ambiguous: lines {_format_lines(best)} all match "
            f"anchors at the same distance from line {target_line}. "
            "Provide a more specific prefix/suffix."
        )

    matched_line = best[0]
    start_offset = _global_offset_for_line(lines, matched_line)
    end_offset = start_offset + len(lines[matched_line - 1])
    return (start_offset, end_offset, matched_line, matched_line)


def _find_line_candidates(
    lines: list[str],
    target_line: int,
    snippet: str,
    radius: int = TEXT_REPLACE_LINE_RADIUS,
) -> list[int]:
    start = max(1, target_line - radius)
    end = min(len(lines), target_line + radius)
    if start > end:
        return []
    return [
        line_no
        for line_no in range(start, end + 1)
        if _line_matches_replace_anchor(lines[line_no - 1], snippet)
    ]


def _line_matches_replace_anchor(line: str, snippet: str) -> bool:
    if snippet == "":
        return line == ""
    return snippet in line


def _rank_line_range_pairs(
    prefix_lines: list[int],
    suffix_lines: list[int],
    start_no: int,
    end_no: int,
) -> list[tuple[tuple[int, int, int, int], int, int]]:
    expected_span = end_no - start_no
    ranked: list[tuple[tuple[int, int, int, int], int, int]] = []
    for prefix_line in prefix_lines:
        for suffix_line in suffix_lines:
            actual_span = suffix_line - prefix_line
            span_delta = abs(actual_span - expected_span)
            if actual_span < 0 or span_delta >= TEXT_REPLACE_SPAN_TOLERANCE:
                continue
            score = (
                abs(prefix_line - start_no) + abs(suffix_line - end_no),
                span_delta,
                abs(prefix_line - start_no),
                abs(suffix_line - end_no),
            )
            ranked.append((score, prefix_line, suffix_line))
    return sorted(ranked)


def _resolve_paragraph_edits(lines: list[str], edits: list[EditEntry]) -> ParagraphResolution | str:
    resolved: list[ResolvedEdit] = []
    for i, edit in enumerate(edits):
        found = _find_paragraph(lines, edit.operation, edit.lineno, edit.prefix, edit.suffix)
        if isinstance(found, str):
            return f"Edit {i}: {found}"
        start_line, end_line = found
        resolved.append(ResolvedEdit(edit.operation, start_line, end_line, edit.new_string))
    return ParagraphResolution(resolved, [])


def _find_paragraph(
    lines: list[str],
    operation: Literal["replace", "insert"],
    lineno: int,
    prefix: str,
    suffix: str,
) -> tuple[int, int] | str:
    del operation
    if lineno == 0 and prefix == "" and suffix == "":
        return (0, 0)
    if prefix == "" or suffix == "":
        return "prefix and suffix must not be empty (except beginning-of-file insertion/prepend with lineno=0)."

    total_lines = len(lines)
    if lineno == 0:
        window_start, window_end = 1, min(total_lines, 100)
    else:
        window_start = max(1, lineno - 100)
        window_end = min(total_lines, lineno + 100)
    if window_start > window_end:
        return f"prefix {prefix!r} not found within ±100 lines of line {lineno}. Read the file to get current content."

    text, line_starts = _window_text(lines, window_start, window_end)
    matches = _find_snippet_matches(text, line_starts, window_start, prefix)
    if not matches:
        return f"prefix {prefix!r} not found within ±100 lines of line {lineno}. Read the file to get current content."

    target_line = 0 if lineno == 0 else lineno
    distances = [abs(match_line - target_line) for _, match_line in matches]
    min_distance = min(distances)
    nearest = [
        match
        for match, distance in zip(matches, distances)
        if distance == min_distance
    ]
    nearest_lines = sorted({line for _, line in nearest})
    if len(nearest_lines) > 1:
        return f"prefix {prefix!r} is ambiguous at lines {_format_lines(nearest_lines)}. Provide a more specific prefix or adjust lineno."

    prefix_offset, start_line = nearest[0]
    suffix_offset = text.find(suffix, prefix_offset + len(prefix))
    if suffix_offset != -1:
        suffix_end_offset = suffix_offset + len(suffix) - 1
        end_line = _line_for_offset(line_starts, window_start, suffix_end_offset)
        if end_line != start_line and prefix == suffix:
            suffix_offset = text.find(suffix, prefix_offset)
    if suffix_offset == -1:
        suffix_offset = text.find(suffix, prefix_offset)
    if suffix_offset == -1:
        return f"suffix {suffix!r} not found after prefix at line {start_line}. Read the file to get current content."
    suffix_end_offset = suffix_offset + len(suffix) - 1
    end_line = _line_for_offset(line_starts, window_start, suffix_end_offset)
    return (start_line, end_line)


def _window_text(lines: list[str], start_line: int, end_line: int) -> tuple[str, list[int]]:
    selected = lines[start_line - 1:end_line]
    starts: list[int] = []
    offset = 0
    for i, line in enumerate(selected):
        starts.append(offset)
        offset += len(line)
        if i < len(selected) - 1:
            offset += 1
    return "\n".join(selected), starts


def _find_snippet_matches(text: str, line_starts: list[int], window_start: int, snippet: str) -> list[tuple[int, int]]:
    matches: list[tuple[int, int]] = []
    offset = text.find(snippet)
    while offset != -1:
        matches.append((offset, _line_for_offset(line_starts, window_start, offset)))
        offset = text.find(snippet, offset + 1)
    return matches


def _global_offset_for_line(lines: list[str], line_number: int) -> int:
    if line_number <= 1:
        return 0
    return len("\n".join(lines[:line_number - 1])) + 1


def _line_for_offset(line_starts: list[int], window_start: int, offset: int) -> int:
    index = bisect_right(line_starts, offset) - 1
    return window_start + max(index, 0)


def _format_lines(lines: list[int]) -> str:
    return ", ".join(str(line) for line in lines)


def _result_trailing_newline(edits: list[ResolvedEdit], total_lines: int, original_trailing_newline: bool) -> bool:
    trailing_newline = original_trailing_newline
    for edit in edits:
        touches_final = (
            edit.operation == "replace" and edit.end_line == total_lines
        ) or (
            edit.operation == "insert" and edit.start_line == total_lines
        )
        if touches_final:
            trailing_newline = edit.new_string.endswith("\n")
    return trailing_newline
