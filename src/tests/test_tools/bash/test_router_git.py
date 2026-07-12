"""Tests for bash_router.py — route hint detection for bash commands."""

from __future__ import annotations

import pytest

from voidx.tools.bash import RouteHint, try_hint


class TestGitCommitCompactForm:
    """git commit with double-quote in args → still hints (git tool accepts any args)."""

    def test_compact_m_flag_double_quoted_hint(self):
        h = try_hint('git commit -m"fix bug"')
        assert h is not None
        assert h.tool_id == "git"

    def test_message_equals_flag(self):
        h = try_hint("git commit --message=fix")
        assert h is not None
        assert "fix" in h.llm_hint

    def test_commit_message_with_dquote_hint(self):
        h = try_hint('git commit -m "she said \\"hi\\""')
        assert h is not None
        assert h.tool_id == "git"


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
    """cat > path << 'EOF' and cat >> path << 'EOF' (append → line)."""

    def test_heredoc_write(self):
        h = try_hint("cat > out.txt << 'EOF'\nhello\nEOF")
        assert h is not None
        assert h.tool_id == "manage"
        assert "hello" in h.llm_hint

    def test_heredoc_append_uses_line(self):
        h = try_hint("cat >> out.txt << 'EOF'\nhello\nEOF")
        assert h is not None
        assert h.tool_id == "write"

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
        valid = {"read", "git", "manage", "write", "replace", "glob", "grep"}
        for cmd, expected_id in [
            ("cat file.py", "read"),
            ("git status", "git"),
            ("echo 'x' > f", "manage"),
            ("sed -i '3s/a/b/' f", "replace"),
            ("echo 'x' >> f", "write"),
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
    """git branch -a/--all and bare branch should hint git tool."""

    def test_branch_a(self):
        h = try_hint("git branch -a")
        assert h is not None
        assert "branch" in h.llm_hint
        assert "-a" in h.llm_hint

    def test_branch_all(self):
        h = try_hint("git branch --all")
        assert h is not None
        assert "branch" in h.llm_hint
        assert "--all" in h.llm_hint

    def test_branch_bare(self):
        h = try_hint("git branch")
        assert h is not None
        assert "branch" in h.llm_hint


class TestGitBranchMutations:
    """git branch create/delete forms should hint git tool."""

    def test_branch_create(self):
        h = try_hint("git branch feature-x")
        assert h is not None
        assert "branch" in h.llm_hint
        assert "feature-x" in h.llm_hint

    def test_branch_delete(self):
        h = try_hint("git branch -d feature-x")
        assert h is not None
        assert "branch" in h.llm_hint
        assert "feature-x" in h.llm_hint

    def test_branch_force_delete(self):
        h = try_hint("git branch -D feature-x")
        assert h is not None
        assert "branch" in h.llm_hint
        assert "-D" in h.llm_hint


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

    def test_diff_staged_alias(self):
        h = try_hint("git diff --staged")
        assert h is not None
        assert "--staged" in h.llm_hint


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

    def test_add_double_dash(self):
        h = try_hint("git add -- src/main.py")
        assert h is not None
        assert "src/main.py" in h.llm_hint

    def test_restore_double_dash(self):
        h = try_hint("git restore -- src/main.py")
        assert h is not None
        assert "src/main.py" in h.llm_hint


class TestGitGlobalOptions:
    """git global options before the subcommand should not suppress hints."""

    def test_git_c_status(self):
        h = try_hint("git -C /tmp status")
        assert h is not None
        assert h.llm_hint == "Prefer git tool with path='/tmp', args='status' for structured output."

    def test_git_no_pager_diff(self):
        h = try_hint("git --no-pager diff")
        assert h is not None
        assert h.llm_hint == "Prefer git tool with args='diff' for structured output."

    def test_git_config_log(self):
        h = try_hint("git -c core.quotepath=false log -5")
        assert h is not None
        assert h.llm_hint == "Prefer git tool with args='log -5' for structured output."


class TestGitTagHints:
    """git tag forms should hint git tool."""

    def test_tag_list(self):
        h = try_hint("git tag")
        assert h is not None
        assert "tag" in h.llm_hint

    def test_tag_list_pattern(self):
        h = try_hint("git tag -l 'v*'")
        assert h is not None
        assert "tag" in h.llm_hint
        assert "v*" in h.llm_hint

    def test_tag_delete(self):
        h = try_hint("git tag -d v1.0.0")
        assert h is not None
        assert "tag" in h.llm_hint
        assert "-d" in h.llm_hint

    def test_tag_create_with_ref(self):
        h = try_hint("git tag v1.0.0 HEAD")
        assert h is not None
        assert "tag" in h.llm_hint
        assert "v1.0.0" in h.llm_hint


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

class TestGitAllSubcommandsHintable:
    """All git subcommands now trigger hint — git tool accepts any args."""

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
    def test_subcommand_triggers_hint(self, cmd):
        h = try_hint(cmd)
        assert h is not None
        assert h.tool_id == "git"


# ---------------------------------------------------------------------------
# Comprehensive: grep flags that cause semantic differences
# ---------------------------------------------------------------------------

