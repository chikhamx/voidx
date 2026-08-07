"""Tests for tool argument formatting for UI display."""

from rich.cells import cell_len

from voidx.presentation.output.console.formatting import _fmt_args, _fmt_args_short
from voidx.presentation.output.manage_display import manage_display


class TestFmtArgsEmptyValues:
    """Empty string and None values should be omitted from display."""

    def test_empty_string_value_omitted(self):
        result = _fmt_args({"args": "status", "path": ""})
        assert "path" not in result
        assert "status" in result

    def test_none_value_omitted(self):
        result = _fmt_args({"args": "status", "path": None})
        assert "path" not in result
        assert "status" in result

    def test_non_empty_string_value_kept(self):
        result = _fmt_args({"args": "status", "path": "src/"})
        assert "path" in result
        assert "src/" in result

    def test_all_values_empty_returns_empty(self):
        result = _fmt_args({"path": "", "args": ""})
        assert result == ""

    def test_mixed_empty_and_non_empty(self):
        result = _fmt_args({"args": "log --oneline", "path": ""})
        assert "log" in result
        assert "path" not in result


class TestFmtArgsArgsKey:
    """The 'args' key should be shown as a bare quoted value, not args="value"."""

    def test_args_key_no_prefix(self):
        result = _fmt_args({"args": "status --porcelain", "path": ""})
        assert "args=" not in result
        assert "status --porcelain" in result

    def test_args_key_with_path(self):
        result = _fmt_args({"args": "log", "path": "src/"})
        assert "args=" not in result
        assert "path=" in result
        assert "src/" in result

    def test_args_key_quoted(self):
        result = _fmt_args({"args": "status"})
        assert result.startswith('"')


class TestManageDisplay:
    def test_create_label_and_value(self):
        assert manage_display({"op": "create", "paths": "src/new.py"}) == ("Create", "src/new.py")
        assert _fmt_args_short("manage", {"op": "create", "paths": "src/new.py"}) == "src/new.py"

    def test_delete_label_is_remove(self):
        assert manage_display({"op": "delete", "paths": "src/old.py"}) == ("Remove", "src/old.py")

    def test_same_directory_move_label_is_rename(self):
        assert manage_display({
            "op": "move",
            "moves": [{"src": "src/old.py", "dest": "src/new.py"}],
        }) == ("Rename", "old.py → new.py")

    def test_cross_directory_move_label_is_move(self):
        assert manage_display({
            "op": "move",
            "moves": [{"src": "src/old.py", "dest": "lib/new.py"}],
        }) == ("Move", "src/old.py → lib/new.py")

    def test_batch_paths_append_count(self):
        assert manage_display({"op": "create", "paths": ["a.py", "b.py", "c.py"]}) == ("Create", "a.py +2")

    def test_long_manage_display_is_one_line_and_truncated(self):
        long_path = "src/" + "very-long-directory-name/" * 5 + "created_file.py"
        label, value = manage_display({"op": "create", "paths": long_path}, limit=42)

        assert label == "Create"
        assert "\n" not in value
        assert cell_len(value) <= 42
        assert "…" in value

    def test_wide_manage_display_is_limited_by_visual_width(self):
        long_path = "src/" + "很长的目录名/" * 8 + "文件.py"
        label, value = manage_display({"op": "create", "paths": long_path}, limit=42)

        assert label == "Create"
        assert "\n" not in value
        assert cell_len(value) <= 42
        assert "…" in value
