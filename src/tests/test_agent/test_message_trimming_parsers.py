"""Tests for message trimming pure functions (rules 1 & 2 helpers)."""

from voidx.agent.message_trimming import (
    parse_read_line_range,
    parse_diff_hunk_ranges,
    merge_ranges,
    coverage_ratio,
    build_diff_spans_from_text,
    summarize_edit_diff,
)


class TestParseReadLineRange:
    def test_single_line(self):
        assert parse_read_line_range("10\tline content") == (10, 10)

    def test_multi_line(self):
        content = "1\tfirst\n2\tsecond\n3\tthird"
        assert parse_read_line_range(content) == (1, 3)

    def test_empty_content(self):
        assert parse_read_line_range("") is None

    def test_no_tab_first_line(self):
        assert parse_read_line_range("not a numbered line") is None

    def test_first_line_not_numbered_but_second_is(self):
        # first line must be parseable; if not, return None
        assert parse_read_line_range("header\n1\tline") is None

    def test_last_line_number_used(self):
        content = "5\ta\n6\tb\n10\tc"
        assert parse_read_line_range(content) == (5, 10)


class TestParseDiffHunkRanges:
    def test_single_hunk(self):
        diff = "File edited: foo.py (1 operations)\n@@ -20,7 +20,12 @@\n ctx\n-old\n+new\n ctx"
        assert parse_diff_hunk_ranges(diff) == [(20, 31)]

    def test_multiple_hunks_non_adjacent(self):
        diff = "@@ -10,3 +10,5 @@\n ctx\n@@ -30,2 +32,4 @@\n ctx"
        assert parse_diff_hunk_ranges(diff) == [(10, 14), (32, 35)]

    def test_multiple_hunks_adjacent_merged(self):
        diff = "@@ -10,3 +10,5 @@\n ctx\n@@ -13,2 +15,3 @@\n ctx"
        assert parse_diff_hunk_ranges(diff) == [(10, 17)]

    def test_pure_deletion_hunk_skipped(self):
        # new_count == 0 → no changed lines in new file
        diff = "@@ -5,3 +5,0 @@\n-old\n-old\n-old"
        assert parse_diff_hunk_ranges(diff) == []

    def test_new_count_omitted_defaults_to_1(self):
        diff = "@@ -20 +20 @@\n ctx\n-old\n+new"
        assert parse_diff_hunk_ranges(diff) == [(20, 20)]

    def test_pure_insert(self):
        diff = "@@ -0,0 +10,3 @@\n+new\n+new\n+new"
        assert parse_diff_hunk_ranges(diff) == [(10, 12)]

    def test_no_hunks(self):
        assert parse_diff_hunk_ranges("File edited: foo.py (1 operations)\n") == []

    def test_all_pure_deletion_yields_empty(self):
        diff = "@@ -5,3 +5,0 @@\n-old\n@@ -10,2 +10,0 @@\n-old"
        assert parse_diff_hunk_ranges(diff) == []


class TestMergeRanges:
    def test_already_sorted_disjoint(self):
        assert merge_ranges([(1, 10), (20, 30)]) == [(1, 10), (20, 30)]

    def test_overlapping_merged(self):
        assert merge_ranges([(1, 10), (5, 15)]) == [(1, 15)]

    def test_adjacent_merged(self):
        assert merge_ranges([(1, 10), (11, 20)]) == [(1, 20)]

    def test_unsorted_input(self):
        assert merge_ranges([(20, 30), (1, 10)]) == [(1, 10), (20, 30)]

    def test_empty(self):
        assert merge_ranges([]) == []

    def test_single(self):
        assert merge_ranges([(5, 5)]) == [(5, 5)]


class TestCoverageRatio:
    def test_full_coverage(self):
        assert coverage_ratio([(1, 100)], [(1, 100)]) == 1.0

    def test_no_coverage(self):
        assert coverage_ratio([(1, 100)], [(200, 300)]) == 0.0

    def test_partial_coverage(self):
        # 82/100
        assert coverage_ratio([(1, 100)], [(10, 30), (40, 100)]) == 0.82

    def test_threshold_60_percent(self):
        # 60/100
        assert coverage_ratio([(1, 100)], [(1, 60)]) == 0.6

    def test_empty_target(self):
        assert coverage_ratio([], [(1, 10)]) == 0.0

    def test_multi_segment_target(self):
        # target [(1,19),(26,95)] = 19+70 = 89 lines
        # union [(1,20)] covers 1-19 = 19 lines
        assert abs(coverage_ratio([(1, 19), (26, 95)], [(1, 20)]) - 19 / 89) < 1e-9


class TestBuildDiffSpansFromText:
    def test_single_hunk(self):
        diff = "@@ -20,7 +20,12 @@\n ctx\n-old\n+new\n ctx"
        spans = build_diff_spans_from_text(diff)
        assert len(spans) == 1
        assert spans[0].old_start == 20
        assert spans[0].old_end == 26  # 20 + 7 - 1
        assert spans[0].offset == 5  # 12 - 7

    def test_multiple_hunks_sorted(self):
        diff = "@@ -30,2 +32,4 @@\n ctx\n@@ -10,3 +10,5 @@\n ctx"
        spans = build_diff_spans_from_text(diff)
        assert len(spans) == 2
        assert spans[0].old_start == 10
        assert spans[1].old_start == 30

    def test_no_hunks(self):
        assert build_diff_spans_from_text("no diff here") == []

    def test_pure_insertion_old_end_lt_old_start(self):
        """P1: pure insertion (old_count==0) must yield old_end < old_start,
        matching file_state.py DiffSpan construction."""
        diff = "@@ -10,0 +10,3 @@\n+new\n+new\n+new"
        spans = build_diff_spans_from_text(diff)
        assert len(spans) == 1
        assert spans[0].old_start == 10
        assert spans[0].old_end == 9  # 10 + 0 - 1
        assert spans[0].offset == 3  # 3 - 0

    def test_pure_insertion_at_start(self):
        """@@ -0,0 +1,3 @@ → old_start=0, old_end=-1."""
        diff = "@@ -0,0 +1,3 @@\n+new\n+new\n+new"
        spans = build_diff_spans_from_text(diff)
        assert len(spans) == 1
        assert spans[0].old_start == 0
        assert spans[0].old_end == -1
        assert spans[0].offset == 3


class TestSummarizeEditDiff:
    def test_single_hunk_summary(self):
        content = (
            "File edited: src/foo.py (1 operations)\n"
            "@@ -20,7 +20,12 @@\n"
            " context line\n"
            "-old line\n"
            "+new line\n"
            " context line\n"
        )
        result = summarize_edit_diff(content)
        assert "File edited: src/foo.py (1 operations)" in result
        assert "Changed lines: 20-31" in result
        assert "@@" not in result
        assert "-old line" not in result
        assert "+new line" not in result

    def test_multiple_hunks_summary(self):
        content = (
            "File edited: foo.py (2 operations)\n"
            "@@ -10,3 +10,5 @@\n ctx\n@@ -30,2 +32,4 @@\n ctx\n"
        )
        result = summarize_edit_diff(content)
        assert "Changed lines: 10-14, 32-35" in result

    def test_preserves_line_shift_hints(self):
        content = (
            "File edited: foo.py (1 operations)\n"
            "Line shift: lines after 30 shifted by +2\n"
            "@@ -20,7 +20,12 @@\n ctx\n"
        )
        result = summarize_edit_diff(content)
        assert "Line shift: lines after 30 shifted by +2" in result

    def test_pure_deletion_no_changed_lines(self):
        content = (
            "File edited: foo.py (1 operations)\n"
            "@@ -5,3 +5,0 @@\n-old\n-old\n-old\n"
        )
        result = summarize_edit_diff(content)
        assert "Changed lines: (deletion only)" in result

    def test_no_hunks_keeps_header(self):
        content = "File edited: foo.py (1 operations)\n"
        result = summarize_edit_diff(content)
        assert result.strip() == "File edited: foo.py (1 operations)"

    def test_blank_lines_dropped(self):
        # fb1: truly empty lines ("") should be dropped, not kept.
        content = (
            "File edited: foo.py (1 operations)\n"
            "\n"
            "@@ -20,7 +20,12 @@\n ctx\n-old\n+new\n ctx\n"
            "\n"
        )
        result = summarize_edit_diff(content)
        assert "File edited: foo.py (1 operations)" in result
        assert "Changed lines: 20-31" in result
        # No blank lines in the kept output (header + Changed lines only)
        assert "\n\n" not in result


