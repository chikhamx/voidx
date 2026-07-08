from __future__ import annotations

from typing import TYPE_CHECKING

from .types import (
    TEXT_REPLACE_LINE_RADIUS,
    TEXT_REPLACE_SPAN_TOLERANCE,
    ResolvedEdit,
)

if TYPE_CHECKING:
    from .state import DiffSpan


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
        return _anchor_not_found_message(lines, start_no, "start_anchor", prefix)

    suffix_lines = _find_line_candidates(lines, end_no, suffix)
    if not suffix_lines:
        return _anchor_not_found_message(lines, end_no, "end_anchor", suffix)

    ranked = _rank_line_range_pairs(prefix_lines, suffix_lines, start_no, end_no)
    if not ranked:
        return (
            "No valid replace range found. "
            f"You specified lines {start_no}-{end_no}, but the closest anchor match covers a different range.\n"
            f"start_anchor {prefix!r} matched on line(s): {_format_lines(prefix_lines)}\n"
            f"end_anchor {suffix!r} matched on line(s): {_format_lines(suffix_lines)}\n"
            "Hint: Read the target block again, then retry replace with the current "
            "start_no/end_no and matching anchors."
        )

    best_score = ranked[0][0]
    best = [item for item in ranked if item[0] == best_score]
    if len(best) > 1:
        ranges = ", ".join(f"{start}-{end}" for _, start, end in best)
        return (
            "replace range is ambiguous: candidate ranges "
            f"{ranges} have the same score.\n"
            "Hint: Provide more specific start_anchor/end_anchor or adjust start_no/end_no."
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
    # Single-line replace with empty start_anchor: trust the line number
    # instead of searching for an empty line. Do NOT call _find_line_candidates
    # with "" — that would match nearby empty lines and could edit the wrong
    # line when the target line is non-empty.
    if prefix == "":
        if target_line < 1 or target_line > len(lines):
            return f"line {target_line} out of range for file with {len(lines)} lines."
        prefix_lines = [target_line]
    else:
        prefix_lines = _find_line_candidates(lines, target_line, prefix)
        if not prefix_lines:
            return _anchor_not_found_message(lines, target_line, "start_anchor", prefix)

    if suffix != "" and suffix != prefix:
        matched_prefix_lines = prefix_lines
        prefix_lines = [
            line_no
            for line_no in prefix_lines
            if _line_matches_replace_anchor(lines[line_no - 1], suffix)
        ]
        if not prefix_lines:
            suffix_matches = _global_anchor_search(lines, suffix)
            candidate_lines = sorted(set(matched_prefix_lines + suffix_matches))
            return (
                f"start_anchor {prefix!r} matched near line {target_line}, "
                f"but end_anchor {suffix!r} is not on the same line.\n"
                "Single-line replace requires both anchors on the same line.\n"
                f"{_format_candidate_lines(lines, candidate_lines)}\n"
                "Hint: If you meant to replace multiple lines, use different "
                "start_no/end_no values so start_anchor matches the first line and "
                "end_anchor matches the last line."
            )

    prefix_lines.sort(key=lambda l: abs(l - target_line))
    best_dist = abs(prefix_lines[0] - target_line)
    best = [l for l in prefix_lines if abs(l - target_line) == best_dist]

    if len(best) > 1:
        return (
            f"single-line match ambiguous: {len(best)} candidate lines match anchors "
            f"at equal distance from line {target_line}:\n"
            f"{_format_candidate_lines(lines, best)}\n"
            "Hint: Provide a longer start_anchor that uniquely identifies the target line."
        )

    matched_line = best[0]
    start_offset = _global_offset_for_line(lines, matched_line)
    end_offset = start_offset + len(lines[matched_line - 1])
    return (start_offset, end_offset, matched_line, matched_line)


def _anchor_not_found_message(lines: list[str], target_line: int, anchor_name: str, anchor: str) -> str:
    if anchor == "":
        actual = ""
        if 1 <= target_line <= len(lines):
            actual = f" — line {target_line} is not empty"
        return (
            f"empty line anchor was not found near line {target_line}{actual}.\n"
            f"Lines around {target_line}:\n{_window_snippet(lines, target_line)}\n"
            f"Hint: If the target line has content, use a substring from that line as {anchor_name} "
            "instead of an empty string."
        )

    matches = _global_anchor_search(lines, anchor)
    base = (
        f"{anchor_name} {anchor!r} not found near line {target_line}.\n"
        f"Lines around {target_line}:\n{_window_snippet(lines, target_line)}"
    )
    if len(matches) == 1:
        matched = matches[0]
        return (
            f"{base}\n"
            f"Hint: {anchor!r} appears on line {matched}. Read lines {matched}-{matched}, "
            "then retry replace with the refreshed line number and anchor."
        )
    if matches:
        return (
            f"{base}\n"
            f"Hint: {anchor!r} appears on lines {_format_lines(matches)} — provide a longer "
            "anchor to identify the target line."
        )
    return (
        f"{base}\n"
        f"Hint: {anchor!r} was not found anywhere in the file. Check for typos or read "
        "the file again before retrying."
    )


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


def _global_anchor_search(lines: list[str], anchor: str) -> list[int]:
    if anchor == "":
        return [line_no for line_no, line in enumerate(lines, start=1) if line == ""]
    return [
        line_no
        for line_no, line in enumerate(lines, start=1)
        if _line_matches_replace_anchor(line, anchor)
    ]


def _line_matches_replace_anchor(line: str, snippet: str) -> bool:
    normalized = next((s for s in snippet.split("\n") if s != ""), "")
    if normalized == "":
        return line == ""
    return normalized in line


def _span_tolerance(expected_span: int) -> int:
    return max(TEXT_REPLACE_SPAN_TOLERANCE, expected_span // 10)


def _rank_line_range_pairs(
    prefix_lines: list[int],
    suffix_lines: list[int],
    start_no: int,
    end_no: int,
) -> list[tuple[tuple[int, int, int, int], int, int]]:
    expected_span = end_no - start_no
    tolerance = _span_tolerance(expected_span)
    ranked: list[tuple[tuple[int, int, int, int], int, int]] = []
    for prefix_line in prefix_lines:
        for suffix_line in suffix_lines:
            actual_span = suffix_line - prefix_line
            span_delta = abs(actual_span - expected_span)
            if actual_span < 0 or span_delta >= tolerance:
                continue
            score = (
                abs(prefix_line - start_no) + abs(suffix_line - end_no),
                span_delta,
                abs(prefix_line - start_no),
                abs(suffix_line - end_no),
            )
            ranked.append((score, prefix_line, suffix_line))
    return sorted(ranked)


def _global_offset_for_line(lines: list[str], line_number: int) -> int:
    if line_number <= 1:
        return 0
    return len("\n".join(lines[:line_number - 1])) + 1


def _format_lines(lines: list[int]) -> str:
    return ", ".join(str(line) for line in lines)


def _format_candidate_lines(lines: list[str], line_numbers: list[int], max_count: int = 5) -> str:
    parts: list[str] = []
    for line_no in line_numbers[:max_count]:
        if 1 <= line_no <= len(lines):
            content = lines[line_no - 1]
            if len(content) > 80:
                content = content[:77] + "..."
            parts.append(f"  line {line_no}: {content}")
    if len(line_numbers) > max_count:
        parts.append(f"  ... {len(line_numbers) - max_count} more")
    return "\n".join(parts)


def _window_snippet(lines: list[str], center: int, radius: int = TEXT_REPLACE_LINE_RADIUS) -> str:
    start = max(1, center - radius)
    end = min(len(lines), center + radius)
    parts: list[str] = []
    for i in range(start, end + 1):
        content = lines[i - 1]
        if len(content) > 80:
            content = content[:77] + "..."
        parts.append(f"  {i}: {content}")
    return "\n".join(parts)


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


def remap_line_range(
    start: int,
    end: int,
    span_steps: "list[list[DiffSpan]]",
) -> tuple[int, int] | None:
    """Project a read-epoch line range through a sequence of edit steps.

    Each step is a list of DiffSpan in the coordinate system of the file
    after the previous step.  Returns the (start, end) in the current file
    coordinate system, or None if the range was fully deleted/replaced or
    split into multiple discontinuous segments.
    """
    from .state import _remap_old_range

    pending: list[tuple[int, int]] = [(start, end)]
    for step in span_steps:
        next_pending: list[tuple[int, int]] = []
        for s, e in pending:
            for item in _remap_old_range(s, e, step):
                next_pending.append((int(item["start_line"]), int(item["end_line"])))
        pending = next_pending
        if not pending:
            return None
    if len(pending) != 1:
        return None
    return pending[0]
