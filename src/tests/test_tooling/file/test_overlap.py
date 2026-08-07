import pytest

from voidx.tooling.domain.file_overlap import LineOverlap, resolve_overlap


def test_resolve_overlap_returns_zero_without_match():
    assert resolve_overlap(["before"], ["new"], ["after"]) == LineOverlap(head=0, tail=0)


@pytest.mark.parametrize("count", [1, 2, 3])
def test_resolve_overlap_matches_largest_head(count):
    matched = [f"head-{index}" for index in range(count)]

    assert resolve_overlap(["keep", *matched], [*matched, "new"], ["after"]) == LineOverlap(
        head=count,
        tail=0,
    )


@pytest.mark.parametrize("count", [1, 2, 3])
def test_resolve_overlap_matches_largest_tail(count):
    matched = [f"tail-{index}" for index in range(count)]

    assert resolve_overlap(["before"], ["new", *matched], [*matched, "keep"]) == LineOverlap(
        head=0,
        tail=count,
    )


def test_resolve_overlap_matches_both_sides():
    assert resolve_overlap(
        ["keep", "head-1", "head-2"],
        ["head-1", "head-2", "new", "tail-1", "tail-2"],
        ["tail-1", "tail-2", "keep"],
    ) == LineOverlap(head=2, tail=2)


def test_resolve_overlap_prioritizes_head_when_both_sides_compete():
    assert resolve_overlap(["same"], ["same"], ["same"]) == LineOverlap(head=1, tail=0)


@pytest.mark.parametrize(
    ("before", "new_lines", "after"),
    [
        ([""], ["", "new"], ["after"]),
        (["before"], ["new", ""], [""]),
    ],
)
def test_resolve_overlap_does_not_consume_empty_lines(before, new_lines, after):
    assert resolve_overlap(before, new_lines, after) == LineOverlap(head=0, tail=0)


@pytest.mark.parametrize(
    ("before", "new_lines", "after"),
    [
        ([" value"], ["value", "new"], ["after"]),
        (["before"], ["new", "value"], ["value "]),
        (["Value"], ["value", "new"], ["after"]),
    ],
)
def test_resolve_overlap_requires_exact_line_match(before, new_lines, after):
    assert resolve_overlap(before, new_lines, after) == LineOverlap(head=0, tail=0)


def test_resolve_overlap_does_not_consume_more_than_limit():
    matched = ["one", "two", "three", "four"]

    assert resolve_overlap(matched, [*matched, "new"], ["after"]) == LineOverlap(head=0, tail=0)


def test_resolve_overlap_handles_file_boundaries():
    assert resolve_overlap([], ["new", "tail"], ["tail"]) == LineOverlap(head=0, tail=1)
    assert resolve_overlap(["head"], ["head", "new"], []) == LineOverlap(head=1, tail=0)


# ── collapse_adjacent_duplicate_blocks (L2) ──────────────────────────

from voidx.tooling.domain.file_overlap import collapse_adjacent_duplicate_blocks


def _collapse(lines, boundaries):
    result = collapse_adjacent_duplicate_blocks(lines, boundaries=boundaries)
    return result.lines, [(b.index, b.size, b.gap) for b in result.collapsed]


def test_collapse_no_duplicate_keeps_lines():
    lines, collapsed = _collapse(["a", "b", "c"], boundaries=[1])
    assert lines == ["a", "b", "c"]
    assert collapsed == []


@pytest.mark.parametrize("size", [2, 3])
def test_collapse_strict_adjacent_block(size):
    block = [f"b{i}" for i in range(size)]
    lines, collapsed = _collapse(["h", *block, *block, "f"], boundaries=[4])
    assert lines == ["h", *block, "f"]
    assert len(collapsed) == 1
    assert collapsed[0][1] == size
    assert collapsed[0][2] == 0


def test_collapse_near_adjacent_single_blank_gap():
    lines, collapsed = _collapse(["h", "A", "B", "", "A", "B", "f"], boundaries=[3])
    assert lines == ["h", "A", "B", "f"]
    assert collapsed[0][2] == 1


def test_collapse_two_blank_gap_not_folded():
    lines, collapsed = _collapse(["h", "A", "B", "", "", "A", "B", "f"], boundaries=[3])
    assert lines == ["h", "A", "B", "", "", "A", "B", "f"]
    assert collapsed == []


def test_collapse_triple_block_folds_to_single():
    lines, collapsed = _collapse(["A", "B", "A", "B", "A", "B"], boundaries=[3])
    assert lines == ["A", "B"]
    assert len(collapsed) == 2


def test_collapse_single_line_repeat_not_folded():
    lines, collapsed = _collapse(["h", "import os", "import os", "f"], boundaries=[2])
    assert lines == ["h", "import os", "import os", "f"]
    assert collapsed == []


def test_collapse_single_line_blank_gap_not_folded():
    lines, collapsed = _collapse(["h", "});", "", "});", "f"], boundaries=[2])
    assert lines == ["h", "});", "", "});", "f"]
    assert collapsed == []


def test_collapse_block_containing_empty_line_not_folded():
    lines, collapsed = _collapse(["A", "", "A", ""], boundaries=[2])
    assert lines == ["A", "", "A", ""]
    assert collapsed == []


def test_collapse_outside_boundaries_untouched():
    lines = ["A", "B", "A", "B", "X", "Y", "Z", "A", "B", "A", "B"]
    out, collapsed = _collapse(lines, boundaries=[5])
    assert out == lines
    assert collapsed == []


def test_collapse_requires_exact_match():
    lines, collapsed = _collapse(["A", "B", "A", "B "], boundaries=[2])
    assert collapsed == []


def test_collapse_dual_windows_cover_head_and_tail():
    # duplicate near head boundary and near tail boundary both folded
    lines = ["A", "B", "A", "B", "m1", "m2", "m3", "m4", "m5", "m6", "m7", "m8", "C", "D", "C", "D"]
    out, collapsed = _collapse(lines, boundaries=[2, 13])
    assert out == ["A", "B", "m1", "m2", "m3", "m4", "m5", "m6", "m7", "m8", "C", "D"]
    assert len(collapsed) == 2


def test_collapse_result_exposes_metadata():
    result = collapse_adjacent_duplicate_blocks(["A", "B", "A", "B"], boundaries=[2])
    assert result.collapsed[0].index == 0
    assert result.collapsed[0].size == 2
    assert result.collapsed[0].gap == 0


def test_collapse_second_window_shifts_after_first_deletion():
    # duplicate blocks in both windows, far apart: after folding the first
    # window, indices in the second window must shift left.
    lines = ["A", "B", "A", "B", "m1", "m2", "m3", "m4", "m5", "m6", "C", "D", "C", "D"]
    out, collapsed = _collapse(lines, boundaries=[2, 12])
    assert out == ["A", "B", "m1", "m2", "m3", "m4", "m5", "m6", "C", "D"]
    assert len(collapsed) == 2
