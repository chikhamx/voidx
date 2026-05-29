import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from voidx.ui.diff import language_from_path, parse_unified_diff, render_file_change_lines


def test_parse_unified_diff_extracts_file_hunks_and_counts():
    diff = """--- a/test.cpp
+++ b/test.cpp
@@ -415,4 +415,5 @@
 // Pure English
-ok &= expect_primary_1("word-en-fallback");
+ok &= expect_primary_full("word-en-fallback");
+more();
 return ok;
"""

    parsed = parse_unified_diff(diff)

    assert len(parsed.files) == 1
    file_diff = parsed.files[0]
    assert file_diff.path == "test.cpp"
    assert file_diff.operation == "Update"
    assert file_diff.added == 2
    assert file_diff.removed == 1
    assert file_diff.hunks[0].lines[1].kind == "remove"
    assert file_diff.hunks[0].lines[1].old_lineno == 416
    assert file_diff.hunks[0].lines[2].new_lineno == 416


def test_render_file_change_lines_uses_summary_and_line_numbers():
    parsed = parse_unified_diff("""--- a/test.cpp
+++ b/test.cpp
@@ -1,2 +1,2 @@
-old
+new
 keep
""")

    lines, omitted = render_file_change_lines(parsed.files[0])
    rendered = "\n".join(lines)

    assert omitted is False
    assert "Added 1 line, removed 1 line" in rendered
    assert "    1 -" in rendered
    assert "    1 +" in rendered
    assert "keep" in rendered


def test_render_file_change_lines_syntax_highlights_code_tokens():
    parsed = parse_unified_diff("""--- a/test.cpp
+++ b/test.cpp
@@ -1,2 +1,2 @@
-return "old", 1;
+return "new", 2;
 // comment
""")

    lines, _ = render_file_change_lines(parsed.files[0])
    rendered = "\n".join(lines)

    assert language_from_path("test.cpp") == "cpp"
    assert "[#ff5caa]return[/#ff5caa]" in rendered
    assert "[#EBCB8B]\"new\"[/#EBCB8B]" in rendered
    assert "[#B48EFD]2[/#B48EFD]" in rendered
    assert "[#7A7F8A]// comment[/#7A7F8A]" in rendered
