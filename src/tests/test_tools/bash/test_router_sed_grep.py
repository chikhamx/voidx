"""Tests for bash_router.py — route hint detection for bash commands."""

from __future__ import annotations

import pytest

from voidx.tools.bash import RouteHint, try_hint


class TestGrepSemanticFlags:
    """grep -v, -l, -c, -A/-B → no hint (semantic difference)."""

    @pytest.mark.parametrize("cmd", [
        "grep -v pattern file.py",
        "grep -l pattern file.py",
        "grep -c pattern file.py",
    ])
    def test_semantic_grep_flags_no_hint(self, cmd):
        assert try_hint(cmd) is None


class TestGrepSupportedFlags:
    """grep -i, -w, -C now map to built-in grep parameters."""

    def test_grep_ignore_case(self):
        h = try_hint("grep -i pattern file.py")
        assert h is not None
        assert h.tool_id == "grep"
        assert "ignore_case=True" in h.llm_hint

    def test_grep_whole_word(self):
        h = try_hint("grep -w pattern file.py")
        assert h is not None
        assert h.tool_id == "grep"
        assert "whole_word=True" in h.llm_hint

    def test_grep_context(self):
        h = try_hint("grep -C1 pattern file.py")
        assert h is not None
        assert h.tool_id == "grep"
        assert "context_lines=1" in h.llm_hint

    def test_grep_context_long(self):
        h = try_hint("grep --context 2 pattern file.py")
        assert h is not None
        assert h.tool_id == "grep"
        assert "context_lines=2" in h.llm_hint


    def test_grep_after_context(self):
        h = try_hint("grep -A2 pattern file.py")
        assert h is not None
        assert h.tool_id == "grep"
        assert "context_lines=2" in h.llm_hint

    def test_grep_before_context(self):
        h = try_hint("grep -B3 pattern file.py")
        assert h is not None
        assert h.tool_id == "grep"
        assert "context_lines=3" in h.llm_hint

    def test_grep_after_and_before_context_takes_max(self):
        h = try_hint("grep -A2 -B5 pattern file.py")
        assert h is not None
        assert h.tool_id == "grep"
        assert "context_lines=5" in h.llm_hint

    def test_grep_exclude_multiple(self):
        h = try_hint("grep --exclude=*.min.js --exclude=*.map pattern")
        assert h is not None
        assert h.tool_id == "grep"
        assert "*.min.js" in h.llm_hint
        assert "*.map" in h.llm_hint

    def test_grep_short_flag_combo(self):
        h = try_hint("grep -in pattern file.py")
        assert h is not None
        assert h.tool_id == "grep"
        assert "ignore_case=True" in h.llm_hint
        assert 'path="file.py"' in h.llm_hint

    def test_grep_e_pattern(self):
        h = try_hint("grep -e pattern file.py")
        assert h is not None
        assert h.tool_id == "grep"
        assert 'pattern="pattern"' in h.llm_hint

    def test_grep_single_quoted_regex_anchor(self):
        h = try_hint("grep 'foo$' file.py")
        assert h is not None
        assert h.tool_id == "grep"
        assert 'pattern="foo$"' in h.llm_hint

# ---------------------------------------------------------------------------
# Comprehensive: basic positive cases
# ---------------------------------------------------------------------------

class TestBasicPositive:
    """Core positive cases for each tool category."""

    def test_cat(self):
        h = try_hint("cat file.py")
        assert h is not None
        assert h.tool_id == "read"
        assert "file.py" in h.llm_hint

    def test_git_status(self):
        h = try_hint("git status")
        assert h is not None
        assert h.tool_id == "git"
        assert "status" in h.llm_hint

    def test_git_diff(self):
        h = try_hint("git diff")
        assert h is not None
        assert "diff" in h.llm_hint

    def test_git_log(self):
        h = try_hint("git log")
        assert h is not None
        assert "log" in h.llm_hint

    def test_git_blame(self):
        h = try_hint("git blame file.py")
        assert h is not None
        assert "blame" in h.llm_hint

    def test_git_remote_v(self):
        h = try_hint("git remote -v")
        assert h is not None
        assert "remote" in h.llm_hint

    def test_git_add(self):
        h = try_hint("git add file.py")
        assert h is not None
        assert "add" in h.llm_hint

    def test_git_restore(self):
        h = try_hint("git restore file.py")
        assert h is not None
        assert "restore" in h.llm_hint

    def test_git_restore_staged(self):
        h = try_hint("git restore --staged file.py")
        assert h is not None
        assert "staged" in h.llm_hint

    def test_find_name(self):
        h = try_hint("find . -name '*.py'")
        assert h is not None
        assert h.tool_id == "glob"
        assert "**/*.py" in h.llm_hint

    def test_find_iname(self):
        h = try_hint("find . -iname '*.py'")
        assert h is not None
        assert h.tool_id == "glob"
        assert "ignore_case=True" in h.llm_hint

    def test_find_maxdepth(self):
        h = try_hint("find . -maxdepth 2 -name '*.py'")
        assert h is not None
        assert h.tool_id == "glob"
        assert "max_depth=2" in h.llm_hint

    def test_grep_basic(self):
        h = try_hint("grep pattern file.py")
        assert h is not None
        assert h.tool_id == "grep"

    def test_grep_recursive(self):
        h = try_hint("grep -r pattern")
        assert h is not None

    def test_rg_basic(self):
        h = try_hint("rg pattern")
        assert h is not None

    def test_sed_simple(self):
        h = try_hint("sed -i '3s/old/new/' file.py")
        assert h is not None
        assert h.tool_id == "replace"

    def test_echo_write(self):
        h = try_hint("echo 'hello' > file.txt")
        assert h is not None
        assert h.tool_id == "file"

    def test_echo_append(self):
        h = try_hint("echo 'hello' >> file.txt")
        assert h is not None
        assert h.tool_id == "write"

    def test_unknown_command_no_hint(self):
        assert try_hint("python3 script.py") is None

    def test_ls_no_hint(self):
        assert try_hint("ls -la") is None


# ---------------------------------------------------------------------------
# Regression: echo/printf content with > inside quotes
# ---------------------------------------------------------------------------

class TestEchoRedirectInContent:
    """echo 'x > y' > file.txt must produce correct path, not split at quoted >."""

    def test_echo_content_with_gt(self):
        h = try_hint("echo 'x > y' > file.txt")
        assert h is not None
        assert h.tool_id == "file"
        assert 'file_path="file.txt"' in h.llm_hint

    def test_echo_content_with_double_gt_append(self):
        h = try_hint("echo 'a >> b' >> file.txt")
        assert h is not None
        assert h.tool_id == "write"
        assert 'file_path="file.txt"' in h.llm_hint

    def test_printf_content_with_gt(self):
        h = try_hint("printf 'x > y' > file.txt")
        assert h is not None
        assert h.tool_id == "file"
        assert 'file_path="file.txt"' in h.llm_hint

    def test_echo_redirect_without_spaces(self):
        h = try_hint("echo 'hello'>file.txt")
        assert h is not None
        assert h.tool_id == "file"
        assert 'file_path="file.txt"' in h.llm_hint

    def test_echo_append_without_spaces(self):
        h = try_hint("echo 'hello'>>file.txt")
        assert h is not None
        assert h.tool_id == "write"
        assert 'file_path="file.txt"' in h.llm_hint


# ---------------------------------------------------------------------------
# Regression: git log -N shorthand
# ---------------------------------------------------------------------------

class TestGitLogShortLimit:
    """git log -5 is shorthand for git log -n 5."""

    def test_git_log_dash_n(self):
        h = try_hint("git log -5")
        assert h is not None
        assert h.tool_id == "git"
        assert "log" in h.llm_hint
        assert "-5" in h.llm_hint

    def test_git_log_dash_n_with_author(self):
        h = try_hint("git log -10 --author=x")
        assert h is not None
        assert "log" in h.llm_hint
        assert "--author=x" in h.llm_hint


# ---------------------------------------------------------------------------
# Regression: sed range delete uses two-step guidance, not <lineN> placeholder
# ---------------------------------------------------------------------------

class TestSedRangeDeleteHint:
    """sed range delete must not use <lineN> as start_anchor/end_anchor placeholder."""

    def test_range_delete_no_placeholder(self):
        h = try_hint("sed -i '10,20d' file.py")
        assert h is not None
        assert "<line" not in h.llm_hint
        assert "first read" in h.llm_hint


class TestSedRegexAnchor:
    """Single-quoted sed scripts containing $ regex anchors should still hint."""

    def test_substitution_with_end_anchor(self):
        h = try_hint("sed -i 's/foo$/bar/' file.py")
        assert h is not None
        assert h.tool_id == "replace"
        assert "foo$" in h.llm_hint


# ---------------------------------------------------------------------------
# cd && prefix stripping
# ---------------------------------------------------------------------------

class TestCdPrefixStripping:
    """cd <dir> && <cmd> should strip the cd prefix and hint on <cmd>."""

    def test_cd_and_sed(self):
        h = try_hint("cd /tmp && sed -i '' 's/old/new/g' file.py")
        assert h is not None
        assert h.tool_id == "replace"

    def test_cd_and_git_status(self):
        h = try_hint("cd /tmp && git status")
        assert h is not None
        assert h.tool_id == "git"

    def test_cd_and_cat(self):
        h = try_hint("cd /tmp && cat file.py")
        assert h is not None
        assert h.tool_id == "read"

    def test_cd_and_grep(self):
        h = try_hint("cd /tmp && grep pattern file.py")
        assert h is not None
        assert h.tool_id == "grep"

    def test_cd_and_find(self):
        h = try_hint("cd /tmp && find . -name '*.py'")
        assert h is not None
        assert h.tool_id == "glob"

    def test_cd_multiple_commands_still_excluded(self):
        """cd && cmd1 && cmd2 should still be excluded (multiple &&)."""
        h = try_hint("cd /tmp && git status && echo done")
        assert h is None

    def test_cd_no_and_no_hint(self):
        """cd without && is not a hintable command."""
        h = try_hint("cd /tmp")
        assert h is None

    def test_bare_and_still_excluded(self):
        """cmd1 && cmd2 without cd prefix should still be excluded."""
        h = try_hint("git status && echo done")
        assert h is None


# ---------------------------------------------------------------------------
# Regression: sed single-line delete (73d) must produce a hint
# ---------------------------------------------------------------------------

class TestSedSingleLineDelete:
    """sed -i '' '73d' file must produce a replace hint (was silently executed)."""

    def test_single_line_delete_macos(self):
        h = try_hint("sed -i '' '73d' file.py")
        assert h is not None
        assert h.tool_id == "replace"

    def test_single_line_delete_linux(self):
        h = try_hint("sed -i '73d' file.py")
        assert h is not None
        assert h.tool_id == "replace"

    def test_single_line_delete_mentions_read_first(self):
        h = try_hint("sed -i '' '73d' file.py")
        assert h is not None
        assert "first read" in h.llm_hint


# ---------------------------------------------------------------------------
# Windows backslash path handling — posix=True eats backslashes
# ---------------------------------------------------------------------------

class TestWindowsBackslashPaths:
    """Route hint detection must preserve backslash paths (Windows).

    shlex posix=True treats backslash as escape char, eating C:\\Users\\foo
    into C:Usersfoo. Must use posix=False to match sandbox.py behavior.
    """

    def test_cat_with_windows_backslash_path_preserves_path(self):
        h = try_hint('cat C:\\Users\\foo\\app.py')
        assert h is not None
        assert h.tool_id == "read"
        assert "C:\\Users\\foo\\app.py" in h.llm_hint

    def test_head_with_windows_backslash_path_preserves_path(self):
        h = try_hint('head C:\\Users\\foo\\app.py')
        assert h is not None
        assert h.tool_id == "read"
        assert "C:\\Users\\foo\\app.py" in h.llm_hint
