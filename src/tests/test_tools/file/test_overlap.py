import pytest

from voidx.tools.file.overlap import LineOverlap, resolve_overlap


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
