"""Tests for bash_router.py — route hint detection for bash commands."""

from __future__ import annotations

import pytest

from voidx.tools.bash import RouteHint, try_hint


class TestGrepSemanticFlags:
    """Unsupported grep forms stay in Bash."""

    @pytest.mark.parametrize("cmd", [
        "grep -v pattern file.py",
        "grep -l pattern file.py",
        "grep -c pattern file.py",
        "grep -A2 pattern file.py",
        "grep -B3 pattern file.py",
        "grep -A2 -B5 pattern file.py",
        "grep -C -1 pattern file.py",
        "rg -r replacement pattern",
        "rg -R pattern",
    ])
    def test_semantic_grep_flags_no_hint(self, cmd):
        assert try_hint(cmd) is None


class TestGrepSupportedFlags:
    """grep -i, -w, -C now map to built-in grep parameters."""

    def test_grep_ignore_case(self):
        h = try_hint("grep -i pattern file.py")
        assert h is not None
        assert h.tool_id == "search"
        assert h.tool_args["case"] == "insensitive"

    def test_grep_whole_word(self):
        h = try_hint("grep -w pattern file.py")
        assert h is not None
        assert h.tool_id == "search"
        assert h.tool_args["match"] == "word"

    def test_grep_context(self):
        h = try_hint("grep -C1 pattern file.py")
        assert h is None

    def test_grep_context_long(self):
        h = try_hint("grep --context 2 pattern file.py")
        assert h is not None
        assert h.tool_args["context"] == 2

    def test_grep_balanced_after_and_before_context(self):
        h = try_hint("grep -A2 -B2 pattern file.py")
        assert h is None

    def test_grep_exclude_multiple(self):
        assert try_hint("grep -r --exclude '*.min.js' --exclude '*.map' pattern .") is None

    def test_grep_without_path_or_recursive_flag_uses_stdin(self):
        assert try_hint("grep pattern") is None

    def test_grep_short_flag_combo(self):
        h = try_hint("grep -in pattern file.py")
        assert h is not None
        assert h.tool_id == "search"
        assert h.tool_args == {
            "query": "pattern",
            "match": "regex",
            "case": "insensitive",
            "path": "file.py",
        }

    def test_grep_recursive_line_number_combo(self):
        h = try_hint("grep -rn pattern src")
        assert h is not None
        assert h.tool_id == "search"
        assert h.tool_args == {
            "query": "pattern",
            "match": "regex",
            "case": "sensitive",
            "path": "src",
        }

    def test_grep_unsupported_short_flag_combo_stays_in_bash(self):
        assert try_hint("grep -rln pattern src") is None

    def test_grep_e_pattern(self):
        assert try_hint("grep -e pattern file.py") is None

    def test_grep_single_quoted_regex_anchor(self):
        h = try_hint("grep 'foo$' file.py")
        assert h is not None
        assert h.tool_id == "search"
        assert h.tool_args["query"] == "foo$"

    @pytest.mark.parametrize("command", [
        r"grep '\(foo\)' file.py",
        "grep '[[:alpha:]]' file.py",
        "egrep '[[:alpha:]]' file.py",
        "rg '[[:alpha:]]'",
    ])
    def test_incompatible_regex_dialect_is_not_routed(self, command):
        assert try_hint(command) is None

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
        h = try_hint("find . -type f -name '*.py'")
        assert h is not None
        assert h.tool_id == "find"
        assert "extensions" in h.llm_hint

    def test_find_iname(self):
        h = try_hint("find . -type f -iname '*.py'")
        assert h is not None
        assert h.tool_id == "find"
        assert "insensitive" in h.llm_hint

    def test_find_maxdepth(self):
        assert try_hint("find . -maxdepth 2 -type f -name '*.py'") is None

    def test_find_without_file_type_is_not_routed(self):
        assert try_hint("find . -name '*.py'") is None

    @pytest.mark.parametrize("command", [
        "find ./../ -type f -name '*.py'",
        "find src -type f -name 'nested/*.py'",
    ])
    def test_find_forms_without_safe_glob_mapping_are_not_routed(self, command):
        assert try_hint(command) is None

    def test_grep_basic(self):
        h = try_hint("grep pattern file.py")
        assert h is not None
        assert h.tool_id == "search"

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
        assert h.tool_id == "manage"

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
        assert h.tool_id == "manage"
        assert 'paths="file.txt"' in h.llm_hint

    def test_echo_content_with_double_gt_append(self):
        h = try_hint("echo 'a >> b' >> file.txt")
        assert h is not None
        assert h.tool_id == "write"
        assert 'file_path="file.txt"' in h.llm_hint

    def test_printf_content_with_gt(self):
        h = try_hint("printf 'x > y' > file.txt")
        assert h is not None
        assert h.tool_id == "manage"
        assert 'paths="file.txt"' in h.llm_hint

    def test_echo_redirect_without_spaces(self):
        h = try_hint("echo 'hello'>file.txt")
        assert h is not None
        assert h.tool_id == "manage"
        assert 'paths="file.txt"' in h.llm_hint

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


class TestSedPrintReadHint:
    """sed -n line print reads should route to the read tool."""

    def test_range_print_routes_to_read(self):
        h = try_hint("sed -n '60,115p' src/voidx/tools/bash/router.py")
        assert h is not None
        assert h.tool_id == "read"
        assert h.tool_args == {
            "file_path": "src/voidx/tools/bash/router.py",
            "offset": 60,
            "limit": 56,
        }

    def test_single_line_print_routes_to_read(self):
        h = try_hint("sed -n '73p' file.py")
        assert h is not None
        assert h.tool_id == "read"
        assert h.tool_args == {"file_path": "file.py", "offset": 73, "limit": 1}

    def test_invalid_range_print_stays_in_bash(self):
        assert try_hint("sed -n '115,60p' file.py") is None


# ---------------------------------------------------------------------------
# cd && prefix stripping
# ---------------------------------------------------------------------------

class TestCdPrefixRouting:
    """Commands that change directory must retain bash working-directory semantics."""

    @pytest.mark.parametrize("command", [
        "cd /tmp && sed -i '' 's/old/new/g' file.py",
        "cd /tmp && git status",
        "cd /tmp && cat file.py",
        "cd /tmp && grep pattern file.py",
        "cd /tmp && find . -type f -name '*.py'",
    ])
    def test_cd_prefixed_command_is_not_routed(self, command):
        assert try_hint(command) is None

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
