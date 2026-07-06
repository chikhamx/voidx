"""Tests for _fmt_args — tool argument formatting for UI display."""

from voidx.ui.output.console.formatting import _fmt_args


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
