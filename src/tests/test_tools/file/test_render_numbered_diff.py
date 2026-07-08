"""Unit tests for render_numbered_diff — covers the design doc test strategy."""

from voidx.diffing import make_structured_diff, render_numbered_diff


def test_context_line_uses_new_lineno_with_space_prefix():
    old = "line1\nline2\nline3\n"
    new = "line1\nCHANGED\nline3\n"
    fd = make_structured_diff("f.txt", old, new)
    out = render_numbered_diff(fd)

    # context line before the change: line1 stays at new_lineno=1
    assert " 1\tline1" in out
    # context line after the change: line3 stays at new_lineno=3
    assert " 3\tline3" in out


def test_add_line_uses_new_lineno_with_plus_prefix():
    old = "line1\nline2\n"
    new = "line1\nline2\nADDED\n"
    fd = make_structured_diff("f.txt", old, new)
    out = render_numbered_diff(fd)

    assert "+3\tADDED" in out


def test_remove_line_uses_old_lineno_with_minus_prefix():
    old = "line1\nline2\nline3\n"
    new = "line1\nline3\n"
    fd = make_structured_diff("f.txt", old, new)
    out = render_numbered_diff(fd)

    assert "-2\tline2" in out


def test_empty_hunks_returns_empty_string():
    old = "line1\nline2\nline3\n"
    fd = make_structured_diff("f.txt", old, old)
    assert render_numbered_diff(fd) == ""


def test_create_file_renders_with_old_start_zero():
    fd = make_structured_diff("new.txt", "", "a\nb\n")
    out = render_numbered_diff(fd)

    assert out.startswith("--- a/new.txt\n+++ b/new.txt\n")
    assert "@@ -0,0 +1,2 @@" in out
    assert "+1\ta" in out
    assert "+2\tb" in out


def test_delete_file_renders_with_new_start_zero():
    fd = make_structured_diff("del.txt", "a\nb\n", "")
    out = render_numbered_diff(fd)

    assert out.startswith("--- a/del.txt\n+++ b/del.txt\n")
    assert "@@ -1,2 +0,0 @@" in out
    assert "-1\ta" in out
    assert "-2\tb" in out
