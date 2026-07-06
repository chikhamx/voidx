"""Tests for make_structured_diff — must match parse_unified_diff(make_file_diff(...))."""

import sys
from pathlib import Path


from voidx.diffing import FileDiff, make_file_diff, make_structured_diff, parse_unified_diff


def _round_trip(filepath: str, old: str, new: str) -> FileDiff:
    """The old path: make_file_diff → parse_unified_diff."""
    diff_text = make_file_diff(filepath, old, new)
    parsed = parse_unified_diff(diff_text)
    return parsed.files[0] if parsed.files else FileDiff(path=filepath)


def _assert_hunks_equal(a: FileDiff, b: FileDiff) -> None:
    assert len(a.hunks) == len(b.hunks), (
        f"hunk count mismatch: structured={len(a.hunks)} vs parsed={len(b.hunks)}"
    )
    for i, (ha, hb) in enumerate(zip(a.hunks, b.hunks)):
        assert ha.old_start == hb.old_start, f"hunk {i} old_start: {ha.old_start} vs {hb.old_start}"
        assert ha.old_count == hb.old_count, f"hunk {i} old_count: {ha.old_count} vs {hb.old_count}"
        assert ha.new_start == hb.new_start, f"hunk {i} new_start: {ha.new_start} vs {hb.new_start}"
        assert ha.new_count == hb.new_count, f"hunk {i} new_count: {ha.new_count} vs {hb.new_count}"
        assert len(ha.lines) == len(hb.lines), (
            f"hunk {i} line count: {len(ha.lines)} vs {len(hb.lines)}"
        )
        for j, (la, lb) in enumerate(zip(ha.lines, hb.lines)):
            assert la.kind == lb.kind, f"hunk {i} line {j} kind: {la.kind} vs {lb.kind}"
            assert la.old_lineno == lb.old_lineno, (
                f"hunk {i} line {j} old_lineno: {la.old_lineno} vs {lb.old_lineno}"
            )
            assert la.new_lineno == lb.new_lineno, (
                f"hunk {i} line {j} new_lineno: {la.new_lineno} vs {lb.new_lineno}"
            )
            assert la.text == lb.text, f"hunk {i} line {j} text: {la.text!r} vs {lb.text!r}"


def test_no_change():
    old = "line1\nline2\nline3\n"
    structured = make_structured_diff("f.txt", old, old)
    parsed = _round_trip("f.txt", old, old)
    assert len(structured.hunks) == 0
    assert len(parsed.hunks) == 0


def test_single_line_replace():
    old = "line1\nline2\nline3\n"
    new = "line1\nREPLACED\nline3\n"
    structured = make_structured_diff("f.txt", old, new)
    parsed = _round_trip("f.txt", old, new)
    _assert_hunks_equal(structured, parsed)


def test_multiline_replace():
    old = "line1\nline2\nline3\nline4\nline5\n"
    new = "line1\nNEW_A\nNEW_B\nNEW_C\nline5\n"
    structured = make_structured_diff("f.txt", old, new)
    parsed = _round_trip("f.txt", old, new)
    _assert_hunks_equal(structured, parsed)


def test_pure_insert_empty_old():
    old = ""
    new = "new1\nnew2\n"
    structured = make_structured_diff("f.txt", old, new)
    parsed = _round_trip("f.txt", old, new)
    _assert_hunks_equal(structured, parsed)
    assert structured.hunks[0].old_start == 0
    assert structured.hunks[0].old_count == 0


def test_pure_delete_empty_new():
    old = "old1\nold2\n"
    new = ""
    structured = make_structured_diff("f.txt", old, new)
    parsed = _round_trip("f.txt", old, new)
    _assert_hunks_equal(structured, parsed)
    assert structured.hunks[0].new_start == 0
    assert structured.hunks[0].new_count == 0


def test_distant_changes_two_hunks():
    old = "\n".join(f"l{i}" for i in range(1, 11)) + "\n"
    new = "\n".join(["X1"] + [f"l{i}" for i in range(2, 10)] + ["X10"]) + "\n"
    structured = make_structured_diff("f.txt", old, new)
    parsed = _round_trip("f.txt", old, new)
    assert len(structured.hunks) == 2
    _assert_hunks_equal(structured, parsed)


def test_insert_at_beginning():
    old = "line1\nline2\n"
    new = "INSERTED\nline1\nline2\n"
    structured = make_structured_diff("f.txt", old, new)
    parsed = _round_trip("f.txt", old, new)
    _assert_hunks_equal(structured, parsed)


def test_delete_last_line():
    old = "line1\nline2\nline3\n"
    new = "line1\nline2\n"
    structured = make_structured_diff("f.txt", old, new)
    parsed = _round_trip("f.txt", old, new)
    _assert_hunks_equal(structured, parsed)


def test_no_trailing_newline():
    old = "line1\nline2\nline3"
    new = "line1\nREPLACED\nline3"
    structured = make_structured_diff("f.txt", old, new)
    parsed = _round_trip("f.txt", old, new)
    _assert_hunks_equal(structured, parsed)


def test_added_removed_counts():
    old = "line1\nline2\nline3\n"
    new = "line1\nNEW_A\nNEW_B\nline3\n"
    structured = make_structured_diff("f.txt", old, new)
    parsed = _round_trip("f.txt", old, new)
    assert structured.added == parsed.added
    assert structured.removed == parsed.removed


def test_both_empty():
    structured = make_structured_diff("f.txt", "", "")
    assert len(structured.hunks) == 0