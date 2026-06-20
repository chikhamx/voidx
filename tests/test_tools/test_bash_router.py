"""Tests for bash_router.py — route hint detection for bash commands."""

from __future__ import annotations

import pytest

from voidx.tools.bash_router import RouteHint, try_hint


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
# #2: sed prefix/suffix hint wording
# ---------------------------------------------------------------------------

class TestSedHintWording:
    """sed hints must explain prefix/suffix are line content anchors."""

    def test_simple_substitution_mentions_anchors(self):
        h = try_hint("sed -i '3s/old/new/' file.py")
        assert h is not None
        assert "prefix/suffix are line content anchors" in h.llm_hint

    def test_global_substitution_mentions_anchors(self):
        h = try_hint("sed -i 's/old/new/g' file.py")
        assert h is not None
        assert "prefix/suffix are line content anchors" in h.llm_hint


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
        assert h.tool_id == "write"

    def test_unquoted_content_no_hint(self):
        assert try_hint("echo hello > file.txt") is None


# ---------------------------------------------------------------------------
# #5: git commit -m"msg" compact form and --message=<msg>
# ---------------------------------------------------------------------------

class TestGitCommitCompactForm:
    """git commit -m"msg" (compact double-quote) → no hint; --message=msg must be handled."""

    def test_compact_m_flag_double_quoted_no_hint(self):
        assert try_hint('git commit -m"fix bug"') is None

    def test_message_equals_flag(self):
        h = try_hint("git commit --message=fix")
        assert h is not None
        assert "fix" in h.llm_hint

    def test_commit_message_with_dquote_no_hint(self):
        assert try_hint('git commit -m "she said \\"hi\\""') is None


# ---------------------------------------------------------------------------
# #6: head -<digits> old-style syntax
# ---------------------------------------------------------------------------

class TestHeadOldStyleDigits:
    """head -5 file must be recognized as head -n 5."""

    def test_head_dash_digits(self):
        h = try_hint("head -5 file.py")
        assert h is not None
        assert h.tool_id == "read"
        assert "limit=5" in h.llm_hint

    def test_head_n_flag_still_works(self):
        h = try_hint("head -n 20 file.py")
        assert h is not None
        assert "limit=20" in h.llm_hint


# ---------------------------------------------------------------------------
# #7: heredoc two orderings and append mode
# ---------------------------------------------------------------------------

class TestHeredocOrderings:
    """cat > path << 'EOF' and cat >> path << 'EOF' (append → insert)."""

    def test_heredoc_write(self):
        h = try_hint("cat > out.txt << 'EOF'\nhello\nEOF")
        assert h is not None
        assert h.tool_id == "write"
        assert "hello" in h.llm_hint

    def test_heredoc_append_uses_insert(self):
        h = try_hint("cat >> out.txt << 'EOF'\nhello\nEOF")
        assert h is not None
        assert h.tool_id == "insert"

    def test_heredoc_content_with_dquote_no_hint(self):
        assert try_hint("cat > out.txt << 'EOF'\nshe said \"hi\"\nEOF") is None

    def test_heredoc_content_over_200_chars_no_hint(self):
        long_content = "x" * 201
        assert try_hint(f"cat > out.txt << 'EOF'\n{long_content}\nEOF") is None


# ---------------------------------------------------------------------------
# #8: tail -n <N> no hint, tail -n +<N> hint
# ---------------------------------------------------------------------------

class TestTailSemantics:
    """tail -n N (last N lines) → no hint; tail -n +N → read(offset=N)."""

    def test_tail_n_plus_offset(self):
        h = try_hint("tail -n +5 file.py")
        assert h is not None
        assert h.tool_id == "read"
        assert "offset=5" in h.llm_hint

    def test_tail_n_last_lines_no_hint(self):
        assert try_hint("tail -n 5 file.py") is None

    def test_tail_bare_no_hint(self):
        assert try_hint("tail file.py") is None


# ---------------------------------------------------------------------------
# #9: RouteHint.tool_id is Literal type
# ---------------------------------------------------------------------------

class TestRouteHintToolIdLiteral:
    """RouteHint.tool_id must be one of the Literal values."""

    def test_all_tool_ids_are_valid(self):
        valid = {"read", "git", "write", "replace", "insert", "glob", "grep"}
        for cmd, expected_id in [
            ("cat file.py", "read"),
            ("git status", "git"),
            ("echo 'x' > f", "write"),
            ("sed -i '3s/a/b/' f", "replace"),
            ("echo 'x' >> f", "insert"),
            ("find . -name '*.py'", "glob"),
            ("grep pattern file.py", "grep"),
        ]:
            h = try_hint(cmd)
            assert h is not None, f"No hint for: {cmd}"
            assert h.tool_id in valid, f"Invalid tool_id {h.tool_id!r} for: {cmd}"
            assert h.tool_id == expected_id, f"Expected {expected_id}, got {h.tool_id} for: {cmd}"


# ---------------------------------------------------------------------------
# #10: metadata route_hint includes command field (tested via BashTool integration)
# This is tested in the BashTool.execute() integration — see test_bash_tool.py
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# #13: git branch -a/--all and git diff -- separator
# ---------------------------------------------------------------------------

class TestGitBranchFlags:
    """git branch -a and git branch --all must hint branch_list with all=true."""

    def test_branch_a(self):
        h = try_hint("git branch -a")
        assert h is not None
        assert "branch_list" in h.llm_hint
        assert '"all": true' in h.llm_hint

    def test_branch_all(self):
        h = try_hint("git branch --all")
        assert h is not None
        assert "branch_list" in h.llm_hint
        assert '"all": true' in h.llm_hint

    def test_branch_bare(self):
        h = try_hint("git branch")
        assert h is not None
        assert "branch_list" in h.llm_hint
        assert "all" not in h.llm_hint


class TestGitDiffSeparator:
    """git diff -- <paths> must extract pathspec after --."""

    def test_diff_double_dash_paths(self):
        h = try_hint("git diff -- src/main.py")
        assert h is not None
        assert "src/main.py" in h.llm_hint

    def test_diff_cached_double_dash(self):
        h = try_hint("git diff --cached -- src/main.py")
        assert h is not None
        assert "cached" in h.llm_hint
        assert "src/main.py" in h.llm_hint


# ---------------------------------------------------------------------------
# #14: git diff -- <paths> separator (covered above in TestGitDiffSeparator)
# Also test git status -- <paths> and git log -- <path>
# ---------------------------------------------------------------------------

class TestGitDoubleDashPaths:
    """git subcommands with -- separator must extract pathspec."""

    def test_status_double_dash(self):
        h = try_hint("git status -- src/main.py")
        assert h is not None
        assert "src/main.py" in h.llm_hint

    def test_log_double_dash(self):
        h = try_hint("git log -- src/main.py")
        assert h is not None
        assert "src/main.py" in h.llm_hint


# ---------------------------------------------------------------------------
# Comprehensive: quick-exclude rules
# ---------------------------------------------------------------------------

class TestQuickExclude:
    """Commands with pipes, semicolons, &&, $, backticks → no hint."""

    @pytest.mark.parametrize("cmd", [
        "cat file | grep pattern",
        "git status && echo done",
        "git status; echo done",
        "cat $HOME/file",
        "cat `find . -name x`",
        "cat $(find . -name x)",
        "git status &",
    ])
    def test_excluded_commands(self, cmd):
        assert try_hint(cmd) is None

    def test_ampersand_in_message_not_excluded(self):
        h = try_hint("git commit -m 'feat: add A & B support'")
        assert h is not None


# ---------------------------------------------------------------------------
# Comprehensive: git unhintable subcommands
# ---------------------------------------------------------------------------

class TestGitUnhintable:
    """git push, pull, merge, etc. → no hint."""

    @pytest.mark.parametrize("cmd", [
        "git push",
        "git pull",
        "git merge main",
        "git rebase main",
        "git cherry-pick abc123",
        "git reset HEAD",
        "git checkout main",
        "git fetch",
        "git clone https://example.com/repo",
        "git init",
    ])
    def test_unhintable_git(self, cmd):
        assert try_hint(cmd) is None


# ---------------------------------------------------------------------------
# Comprehensive: grep flags that cause semantic differences
# ---------------------------------------------------------------------------

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
        assert "remote_list" in h.llm_hint

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
        assert h.tool_id == "write"

    def test_echo_append(self):
        h = try_hint("echo 'hello' >> file.txt")
        assert h is not None
        assert h.tool_id == "insert"

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
        assert h.tool_id == "write"
        assert 'file_path="file.txt"' in h.llm_hint

    def test_echo_content_with_double_gt_append(self):
        h = try_hint("echo 'a >> b' >> file.txt")
        assert h is not None
        assert h.tool_id == "insert"
        assert 'file_path="file.txt"' in h.llm_hint

    def test_printf_content_with_gt(self):
        h = try_hint("printf 'x > y' > file.txt")
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
        assert '"limit": 5' in h.llm_hint

    def test_git_log_dash_n_with_author(self):
        h = try_hint("git log -10 --author=x")
        assert h is not None
        assert '"limit": 10' in h.llm_hint
        assert '"author": "x"' in h.llm_hint


# ---------------------------------------------------------------------------
# Regression: sed range delete uses two-step guidance, not <lineN> placeholder
# ---------------------------------------------------------------------------

class TestSedRangeDeleteHint:
    """sed range delete must not use <lineN> as prefix/suffix placeholder."""

    def test_range_delete_no_placeholder(self):
        h = try_hint("sed -i '10,20d' file.py")
        assert h is not None
        assert "<line" not in h.llm_hint
        assert "first read" in h.llm_hint


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