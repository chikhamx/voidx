"""Tests for direct git command execution in Bash without GitTool routing."""

from __future__ import annotations

import subprocess
import pytest

from voidx.tooling.application.execution import ShellToolContext as ToolContext
from voidx.tooling.domain.authorization import AuthorizationContext
from voidx.tooling.builtin.shell.bash.tool import BashTool
from voidx.tooling.policy.shell.policy import classify_shell_risk, shell_policy_for_command
from voidx.tooling.domain.risk import RiskLevel


def _init_repo(path):
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True, capture_output=True)
    (path / "file.txt").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(path), check=True, capture_output=True)


class TestShellGitPolicy:
    """Git commands have proper safety policy classification in shell policy."""

    def test_git_read_only_commands_allowed(self):
        for cmd in [
            "git status",
            "git status --porcelain",
            "git diff",
            "git log -n 5",
            "git show HEAD",
            "git branch",
            "git rev-parse HEAD",
        ]:
            policy = shell_policy_for_command(cmd, shell="bash")
            assert policy.allowed is True, f"Expected {cmd} to be allowed"
            assert policy.read_only is True, f"Expected {cmd} to be read_only"

            risk = classify_shell_risk(cmd, shell="bash")
            assert risk.level != RiskLevel.BLOCKED, f"Expected {cmd} not to be blocked"

    def test_git_destructive_commands_blocked(self):
        destructive = [
            "git reset --hard",
            "git reset --hard HEAD~1",
            "git clean -x -f",
            "git clean -x",
            "git clean -X", 
            "git clean -fdx",
            "git clean -f -x",
        ]
        for cmd in destructive:
            risk = classify_shell_risk(cmd, shell="bash")
            assert risk.level == RiskLevel.BLOCKED, f"Expected {cmd} to be blocked by shell risk policy"


class TestBashGitDirectExecution:
    """Bash tool executes git commands directly without routing to GitTool."""

    def test_sort_pipeline_is_explicitly_read_only(self):
        policy = shell_policy_for_command("ls | sort | head", shell="bash")

        assert policy.allowed is True
        assert policy.read_only is True

    @pytest.mark.asyncio
    async def test_git_unsafe_read_classification_still_executes_as_bash(self, tmp_path):
        tool = BashTool()
        ctx = ToolContext(workspace=str(tmp_path))

        result = await tool.execute(
            {"command": "git -c core.quotePath=false status"},
            ctx,
        )

        assert result.metadata.get("routed_from") is None
        assert "not a git repository" in result.output.lower()

    @pytest.mark.asyncio
    async def test_git_status_raw_text_output(self, tmp_path):
        _init_repo(tmp_path)
        tool = BashTool()
        ctx = ToolContext(
            workspace=str(tmp_path),
            authorization=AuthorizationContext(permission_mode="full_access"),
        )
        result = await tool.execute({"command": "git status"}, ctx)

        assert result.metadata.get("routed_from") is None
        assert result.metadata.get("exit_code") == 0
        assert "On branch" in result.output
        assert "nothing to commit" in result.output
        # Ensure it's not JSON formatted
        assert not result.output.strip().startswith("{")

    @pytest.mark.asyncio
    async def test_git_with_C_flag_runs_direct(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        (repo / "new_file.txt").write_text("untracked")

        tool = BashTool()
        ctx = ToolContext(
            workspace=str(tmp_path),
            authorization=AuthorizationContext(permission_mode="full_access"),
        )
        result = await tool.execute({"command": f"git -C {repo} status --porcelain"}, ctx)

        assert result.metadata.get("routed_from") is None
        assert result.metadata.get("exit_code") == 0
        assert "??" in result.output
        assert "new_file.txt" in result.output
        assert not result.output.strip().startswith("{")

    @pytest.mark.asyncio
    async def test_git_reset_hard_blocked_by_bash(self, tmp_path):
        _init_repo(tmp_path)
        tool = BashTool()
        ctx = ToolContext(
            workspace=str(tmp_path),
            authorization=AuthorizationContext(permission_mode="full_access"),
        )
        result = await tool.execute({"command": "git reset --hard"}, ctx)

        assert result.metadata.get("blocked") is True
        assert "is blocked" in result.output

    @pytest.mark.asyncio
    async def test_git_stderr_redirect_runs_direct(self, tmp_path):
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        tool = BashTool()
        ctx = ToolContext(
            workspace=str(empty_dir),
            authorization=AuthorizationContext(permission_mode="full_access"),
        )
        result = await tool.execute({"command": "git status 2>&1"}, ctx)

        assert result.metadata.get("routed_from") is None
        assert result.metadata.get("ok") is False
        assert "not a git repository" in result.output.lower()

    def test_git_push_force_variants_blocked(self):
        blocked_push_cmds = [
            "git push -f origin main",
            "git push --force origin master",
            "git push origin main -f",
            "git push origin master --force",
            "git push -u origin main --force",
            "git -C /repo push origin main -f",
        ]
        for cmd in blocked_push_cmds:
            risk = classify_shell_risk(cmd, shell="bash")
            assert risk.level == RiskLevel.BLOCKED, f"Expected {cmd} to be blocked"

    def test_git_destructive_with_global_flags_blocked(self):
        blocked_cmds = [
            "git -C /repo reset --hard",
            "git -C . reset --hard HEAD",
            "git -C /repo clean -fdx",
            "git -C . clean -f -x",
            "git -C /repo clean -fdX",
            "git -C /repo prune",
            "git -C . filter-branch --tree-filter 'rm -f passwords.txt' HEAD",
        ]
        for cmd in blocked_cmds:
            risk = classify_shell_risk(cmd, shell="bash")
            assert risk.level == RiskLevel.BLOCKED, f"Expected {cmd} to be blocked"

    def test_git_config_override_and_dangerous_options_not_read_only(self):
        injections = [
            "git -c core.pager='touch /tmp/pwn' log",
            "git -c alias.status='!rm -rf /' status",
            "git --config-env=core.fsmonitor=EVIL status",
            "git --exec-path=/tmp/evil status",
            "git diff --output=/etc/cron.d/job",
            "git log --output=/tmp/out",
            "GIT_PAGER='touch /tmp/pwn' git log",
            "GIT_EXTERNAL_DIFF='touch /tmp/pwn' git diff",
            "git notes add -m 'note'",
            "git remote add -v origin https://example.com",
        ]
        for cmd in injections:
            policy = shell_policy_for_command(cmd, shell="bash")
            assert policy.read_only is False, f"Expected {cmd} NOT to be classified as read_only"


    def test_git_unknown_read_only_arguments_are_not_safe(self):
        commands = [
            "git status --unknown-option",
            "git log --unknown-option",
            "git diff --unknown-option",
            "git show --unknown-option",
        ]
        for cmd in commands:
            policy = shell_policy_for_command(cmd, shell="bash")
            assert policy.read_only is False, f"Expected {cmd} NOT to be classified as read_only"

    def test_git_environment_injection_is_not_safe_in_bash_capability(self):
        from voidx.tooling.policy.permission.rules import is_safe_bash

        assert is_safe_bash("GIT_PAGER='touch /tmp/pwn' git log") is False
        assert is_safe_bash("env GIT_EXTERNAL_DIFF='touch /tmp/pwn' git diff") is False

    def test_git_access_paths_keep_global_option_values(self):
        from voidx.tooling.policy.shell.policy import _git_access_paths
        from voidx.tooling.policy.permission.rules import shell_words

        words = shell_words("git -C /repo --git-dir=.git status")
        assert words is not None
        assert [str(path) for path in _git_access_paths(words)] == ["/repo", ".git"]

    def test_git_read_only_external_path_uses_read_grant(self, tmp_path):
        from voidx.tooling.application.authorization import PermissionContext
        from voidx.tooling.domain.grants import AccessGrants
        from voidx.tooling.domain.process_sandbox import ProcessSandboxBackend, ProcessSandboxCapability
        from voidx.tooling.policy.shell.policy import shell_sandbox_precheck

        workspace = tmp_path / "workspace"
        external = tmp_path / "external"
        workspace.mkdir()
        external.mkdir()
        target = external / "input.txt"
        target.write_text("input\n")
        action, reason = shell_sandbox_precheck(
            {"command": f"cat {target}"},
            PermissionContext(
                workspace=str(workspace),
                access_grants=AccessGrants.from_parts(readable_dirs=[str(external)]),
                process_sandbox=ProcessSandboxCapability(backend=ProcessSandboxBackend.TEST, supported=True),
            ),
        )

        assert (action, reason) == ("allow", None)

    def test_powershell_git_write_is_not_read_only(self):
        from voidx.tooling.policy.shell.powershell_sandbox import is_safe_powershell_command

        assert is_safe_powershell_command("git status") is True
        assert is_safe_powershell_command("git commit -m x") is False
        assert is_safe_powershell_command("git push origin main") is False
        assert is_safe_powershell_command("git -c core.pager=x log") is False

    def test_direct_git_tool_calls_are_not_a_permission_capability(self):
        from voidx.tooling.policy.permission.rules import PermissionCapability, classify_tool_call

        classified = classify_tool_call({"name": "git", "args": {"args": "status"}})
        assert classified.name == "git"
        assert classified.capability == PermissionCapability.OTHER
        assert classified.pattern == "*"


class TestGitBoundaryClassification:
    def test_global_options_and_wrappers_reach_hard_block_policy(self):
        blocked = [
            "git -C . reset --hard",
            "git --git-dir=.git clean -fdX",
            "GIT_DIR=.git git prune",
            "env GIT_DIR=.git git gc",
        ]

        for command in blocked:
            risk = classify_shell_risk(command, shell="bash")
            assert risk.level == RiskLevel.BLOCKED, f"Expected {command} to be blocked"

    def test_git_read_only_requires_strict_arguments(self):
        not_read_only = [
            "FOO=bar git status",
            "git diff --output /tmp/out",
            "git log --ext-diff",
            "git remote show origin --no-such-option",
            "git notes add -m note",
        ]

        for command in not_read_only:
            policy = shell_policy_for_command(command, shell="bash")
            assert policy.read_only is False, f"Expected {command} not to be read-only"

    def test_git_remote_read_commands_are_explicit(self):
        for command in ("git remote", "git remote -v", "git remote show origin", "git remote get-url origin"):
            policy = shell_policy_for_command(command, shell="bash")
            assert policy.allowed is True
            assert policy.read_only is True

    def test_powershell_git_compounds_are_not_read_only(self):
        from voidx.tooling.policy.shell.powershell_sandbox import is_safe_powershell_command

        assert is_safe_powershell_command("git status; git commit -m x") is False
        assert is_safe_powershell_command("git status | git push origin main") is False

    def test_git_access_paths_are_not_write_grants_for_reads(self, tmp_path):
        from voidx.tooling.application.authorization import PermissionContext
        from voidx.tooling.domain.grants import AccessGrants
        from voidx.tooling.domain.process_sandbox import ProcessSandboxBackend, ProcessSandboxCapability
        from voidx.tooling.policy.shell.policy import shell_sandbox_precheck

        workspace = tmp_path / "workspace"
        external = tmp_path / "external"
        workspace.mkdir()
        external.mkdir()
        (external / "repo").mkdir()
        action, reason = shell_sandbox_precheck(
            {"command": f"git -C {external / 'repo'} status"},
            PermissionContext(
                workspace=str(workspace),
                access_grants=AccessGrants.from_parts(readable_dirs=[str(external)]),
                process_sandbox=ProcessSandboxCapability(backend=ProcessSandboxBackend.TEST, supported=True),
            ),
        )

        assert (action, reason) == ("allow", None)

    def test_git_write_paths_extracts_repo_options_and_outputs(self):
        from voidx.tooling.policy.shell.policy import _git_write_paths
        from voidx.tooling.policy.permission.rules import shell_words

        words = shell_words("git -C /repo --git-dir=.git commit -m msg")
        assert words is not None
        assert [str(p) for p in _git_write_paths(words)] == ["/repo", ".git"]

        diff_words = shell_words("git diff --output=/tmp/diff.patch")
        assert diff_words is not None
        assert [str(p) for p in _git_write_paths(diff_words)] == ["/tmp/diff.patch"]

        checkout_words = shell_words("git checkout -- file.txt")
        assert checkout_words is not None
        assert [str(p) for p in _git_write_paths(checkout_words)] == ["file.txt"]

    def test_git_hard_block_bypasses_blocked(self):
        bypasses = [
            "git push origin +main",
            "git push origin +master",
            "git push origin HEAD:+main",
            "git push origin +refs/heads/main",
            "git push --force-with-lease=main origin main",
            "git push origin :main",
            "git push origin :master",
            "git push origin --delete main",
            "git push -d origin master",
            "git push -vd origin master",
            "git push -f origin --all",
            "git push --force origin --mirror",
            "git.exe reset --hard",
            "git status\ngit reset --hard",
            "git status;git reset --hard",
            "git status&&git reset --hard",
            "(git reset --hard)",
        ]
        for cmd in bypasses:
            risk = classify_shell_risk(cmd, shell="bash")
            assert risk.level == RiskLevel.BLOCKED, f"Expected {cmd!r} to be blocked"

    def test_git_readonly_tightening_and_executables(self):
        assert shell_policy_for_command("git.exe status", shell="bash").allowed is True
        assert shell_policy_for_command("git.exe status", shell="bash").read_only is True
        assert shell_policy_for_command("/usr/bin/git status", shell="bash").allowed is True
        assert shell_policy_for_command("/usr/bin/git status", shell="bash").read_only is True
        assert shell_policy_for_command("git -p status", shell="bash").allowed is True
        assert shell_policy_for_command("git -p status", shell="bash").read_only is True
        assert shell_policy_for_command("git --bare status", shell="bash").allowed is True
        assert shell_policy_for_command("git --bare status", shell="bash").read_only is True
        assert shell_policy_for_command("git.exe status", shell="powershell").allowed is True
        assert shell_policy_for_command("git.exe status", shell="powershell").read_only is True
        assert shell_policy_for_command("git log -1", shell="bash").allowed is True
        assert shell_policy_for_command("git log -1", shell="bash").read_only is True
        assert shell_policy_for_command("git log -5", shell="bash").allowed is True
        assert shell_policy_for_command("git log -5", shell="bash").read_only is True
        assert shell_policy_for_command("git rev-list -1 HEAD", shell="bash").allowed is True
        assert shell_policy_for_command("git rev-list -1 HEAD", shell="bash").read_only is True
        assert shell_policy_for_command("git status -1", shell="bash").read_only is False

    def test_sandbox_read_external_path_without_process_sandbox(self, tmp_path):
        from voidx.tooling.application.authorization import PermissionContext
        from voidx.tooling.domain.grants import AccessGrants
        from voidx.tooling.policy.shell.policy import shell_sandbox_precheck

        workspace = tmp_path / "workspace"
        external = tmp_path / "external"
        workspace.mkdir()
        external.mkdir()
        target = external / "data.txt"
        target.write_text("hello")

        # Without process sandbox capability provided, external path still defers without grant
        ctx_no_grant = PermissionContext(workspace=str(workspace))
        action, reason = shell_sandbox_precheck({"command": f"cat {target}"}, ctx_no_grant)
        assert action == "defer"
        assert "access grant" in (reason or "")

        # With readable grant, external path is allowed even without process sandbox capability
        ctx_with_grant = PermissionContext(
            workspace=str(workspace),
            access_grants=AccessGrants.from_parts(readable_files=[str(target)]),
        )
        action, reason = shell_sandbox_precheck({"command": f"cat {target}"}, ctx_with_grant)
        assert action == "allow"
        assert reason is None
