"""Tests for bash_router.py — route hint detection for bash commands."""

from __future__ import annotations

import pytest

from voidx.tools.bash import RouteHint, try_hint


# ---------------------------------------------------------------------------
# #1: try_hint catches all exceptions, never propagates
# ---------------------------------------------------------------------------

class TestTryHintExceptionSafety:
    """try_hint must never raise — it catches all exceptions and returns None."""

    def test_malformed_command_no_crash(self):
        assert try_hint("") is None

    def test_unbalanced_quotes_no_crash(self):
        assert try_hint("cat 'unclosed") is None

    def test_null_bytes_no_crash(self):
        # null bytes are unusual but shlex handles them; try_hint must not crash
        result = try_hint("cat \x00file")
        assert result is None or isinstance(result, RouteHint)


# ---------------------------------------------------------------------------
# #2: sed start_anchor/end_anchor hint wording
# ---------------------------------------------------------------------------

class TestSedHintWording:
    """sed hints must reference replace with start_anchor/end_anchor."""

    def test_simple_substitution_mentions_replace(self):
        h = try_hint("sed -i '3s/old/new/' file.py")
        assert h is not None
        assert "replace(" in h.llm_hint

    def test_global_substitution_mentions_replace(self):
        h = try_hint("sed -i 's/old/new/g' file.py")
        assert h is not None
        assert "replace(" in h.llm_hint


class TestSedSlashInPattern:
    """sed with escaped slashes or alternate delimiters in pattern."""

    def test_global_substitution_escaped_slash(self):
        h = try_hint("sed -i 's/assert \"Exploring (1\\/3)\"/assert New/g' file.py")
        assert h is not None
        assert h.tool_id == "replace"
        assert "Exploring" in h.llm_hint

    def test_global_substitution_pipe_delimiter(self):
        h = try_hint("sed -i 's|assert \"Exploring (1/3)\"|assert New|g' file.py")
        assert h is not None
        assert h.tool_id == "replace"
        assert "Exploring" in h.llm_hint

    def test_global_substitution_hash_delimiter(self):
        h = try_hint("sed -i 's#old/path#new/path#g' file.py")
        assert h is not None
        assert h.tool_id == "replace"
        assert "old/path" in h.llm_hint


# ---------------------------------------------------------------------------
# #3: echo/printf content with double quotes → no hint
# ---------------------------------------------------------------------------

class TestEchoDoubleQuoteSafety:
    """echo/printf with double-quoted content or content containing " → no hint."""

    def test_double_quoted_content_no_hint(self):
        assert try_hint('echo "hello" > file.txt') is None

    def test_single_quoted_content_with_embedded_dquote_no_hint(self):
        assert try_hint("echo 'she said \"hi\"' > file.txt") is None

    def test_single_quoted_simple_content_hint(self):
        h = try_hint("echo 'hello world' > file.txt")
        assert h is not None
        assert h.tool_id == "file"

    def test_unquoted_content_no_hint(self):
        assert try_hint("echo hello > file.txt") is None


# ---------------------------------------------------------------------------
# #5: git commit -m"msg" compact form and --message=<msg>
# ---------------------------------------------------------------------------

