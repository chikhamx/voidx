"""Tests for bash/powershell auto-route to git tool via maybe_route_hint."""

from __future__ import annotations

from tests.tool_registry import build_registry
import json
import os

import pytest

from voidx.tooling.domain.authorization import AuthorizationContext
from voidx.tooling.application.execution import ShellToolContext as ToolContext
from voidx.tooling.application.registry import ToolRegistry
from voidx.tooling.builtin.shell.common import RouteHint
from voidx.tooling.builtin.shell.bash.router import try_hint


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(tmp_path, registry: ToolRegistry | None = None) -> ToolContext:
    return ToolContext(
        workspace=str(tmp_path),
        authorization=AuthorizationContext(permission_mode="full_access"),
        tool_invoker=registry,
    )


def _make_registry() -> ToolRegistry:
    return build_registry()


# ---------------------------------------------------------------------------
# 1. bash git auto-route 结构化输出
# ---------------------------------------------------------------------------


class TestBashGitAutoRouteStructured:
    """bash git command auto-routes to git tool and returns structured output."""

    @pytest.mark.asyncio
    async def test_git_status_structured(self, tmp_path):
        import subprocess
        subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(tmp_path), check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(tmp_path), check=True, capture_output=True,
        )
        (tmp_path / "file.txt").write_text("hello")
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=str(tmp_path), check=True, capture_output=True,
        )

        r = _make_registry()
        ctx = _make_ctx(tmp_path, r)
        result = await r.execute_tool("bash", {"command": "git status --porcelain"}, ctx)

        data = json.loads(result.output)
        assert "ok" in data
        assert data["ok"] is True
        assert result.metadata.get("routed_from") == "bash"
        assert result.metadata.get("routed_command") == "git status --porcelain"

    @pytest.mark.asyncio
    async def test_git_log_structured(self, tmp_path):
        import subprocess
        subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(tmp_path), check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(tmp_path), check=True, capture_output=True,
        )
        (tmp_path / "file.txt").write_text("hello")
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=str(tmp_path), check=True, capture_output=True,
        )

        r = _make_registry()
        ctx = _make_ctx(tmp_path, r)
        result = await r.execute_tool("bash", {"command": "git log --oneline -1"}, ctx)

        data = json.loads(result.output)
        assert data["ok"] is True
        assert result.metadata.get("routed_from") == "bash"


class TestBashGitShellSyntaxFallback:
    """Shell syntax around git commands should run in bash, not the git tool."""

    @pytest.mark.asyncio
    async def test_git_stderr_redirect_runs_as_bash(self, tmp_path):
        r = _make_registry()
        ctx = _make_ctx(tmp_path, r)

        result = await r.execute_tool("bash", {"command": "git status 2>&1"}, ctx)
        data = json.loads(result.output)

        assert result.metadata.get("routed_from") is None
        assert data["ok"] is False
        assert "not a git repository" in data["stdout"].lower()
        assert data["stderr"] == ""


# ---------------------------------------------------------------------------
# 2. bash git auto-route 带路径
# ---------------------------------------------------------------------------


class TestBashGitAutoRouteWithPath:
    """bash git -C <path> auto-routes with path passed to git tool."""

    @pytest.mark.asyncio
    async def test_git_with_C_flag_routes_with_path(self, tmp_path):
        import subprocess
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(repo), check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(repo), check=True, capture_output=True,
        )
        (repo / "file.txt").write_text("hello")
        subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=str(repo), check=True, capture_output=True,
        )

        r = _make_registry()
        ctx = _make_ctx(tmp_path, r)
        result = await r.execute_tool(
            "bash", {"command": f"git -C {repo} status --porcelain"}, ctx
        )

        data = json.loads(result.output)
        assert data["ok"] is True
        assert result.metadata.get("routed_from") == "bash"
        assert result.metadata.get("routed_tool_args", {}).get("path") == str(repo)


# ---------------------------------------------------------------------------
# 3. bash git 破坏性命令拦截
# ---------------------------------------------------------------------------


class TestBashGitDestructiveDenied:
    """Destructive git commands are denied by git tool policy even via auto-route."""

    @pytest.mark.asyncio
    async def test_git_reset_hard_denied(self, tmp_path):
        import subprocess
        subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(tmp_path), check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(tmp_path), check=True, capture_output=True,
        )
        (tmp_path / "file.txt").write_text("hello")
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=str(tmp_path), check=True, capture_output=True,
        )

        r = _make_registry()
        ctx = _make_ctx(tmp_path, r)
        result = await r.execute_tool("bash", {"command": "git reset --hard"}, ctx)

        data = json.loads(result.output)
        assert data["ok"] is False
        assert "command_denied" in data.get("error", "")
        assert result.metadata.get("routed_from") == "bash"


# ---------------------------------------------------------------------------
# 4. bash 非 git 命令不受影响
# ---------------------------------------------------------------------------


class TestBashNonGitUnaffected:
    """Non-git bash commands still execute normally."""

    @pytest.mark.asyncio
    async def test_echo_hello_not_routed(self, tmp_path):
        r = _make_registry()
        ctx = _make_ctx(tmp_path, r)
        result = await r.execute_tool("bash", {"command": "echo hello"}, ctx)

        data = json.loads(result.output)
        assert data["ok"] is True
        assert "hello" in data["stdout"]
        assert "routed_from" not in result.metadata


# ---------------------------------------------------------------------------
# 5. bash hint 无 registry 时降级
# ---------------------------------------------------------------------------


class TestBashHintFallbackNoRegistry:
    """When ctx.tool_registry is None, git runs as a raw bash command."""

    @pytest.mark.asyncio
    async def test_no_registry_runs_as_bash(self, tmp_path):
        r = _make_registry()
        ctx = _make_ctx(tmp_path, registry=None)
        result = await r.execute_tool("bash", {"command": "git status"}, ctx)

        assert result.metadata.get("skipped") is not True
        assert "not a git repository" in result.output.lower()
        assert "routed_from" not in result.metadata


# ---------------------------------------------------------------------------
# 6. bash read auto-route
# ---------------------------------------------------------------------------


class TestBashReadAutoRoute:
    """bash read-like commands auto-route to read tool."""

    @pytest.mark.asyncio
    async def test_cat_routes_to_read_and_records_coverage(self, tmp_path):
        target = tmp_path / "code.py"
        target.write_text("keep\nREMOVE_ME\n", encoding="utf-8")
        r = _make_registry()
        ctx = _make_ctx(tmp_path, r)

        read_result = await r.execute_tool("bash", {"command": "cat code.py"}, ctx)
        replace_result = await r.execute_tool(
            "replace",
            {
                "file_path": "code.py",
                "bounds": [{"line_no": 2, "anchor": "REMOVE_ME"}],
                "new_string": "",
            },
            ctx,
        )

        assert read_result.metadata.get("tool") == "read"
        assert read_result.metadata.get("routed_from") == "bash"
        assert read_result.metadata.get("routed_tool_args") == {"file_path": "code.py"}
        assert "1\tkeep" in read_result.output
        assert replace_result.metadata.get("error") is not True
        assert target.read_text(encoding="utf-8") == "keep\n"

    @pytest.mark.asyncio
    async def test_head_routes_to_read_limit(self, tmp_path):
        (tmp_path / "code.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
        r = _make_registry()
        ctx = _make_ctx(tmp_path, r)

        result = await r.execute_tool("bash", {"command": "head -n 2 code.py"}, ctx)

        assert result.metadata.get("tool") == "read"
        assert result.metadata.get("routed_tool_args") == {"file_path": "code.py", "limit": 2}
        assert "1\tone" in result.output
        assert "2\ttwo" in result.output
        assert "3\tthree" not in result.output

    @pytest.mark.asyncio
    async def test_tail_plus_routes_to_read_offset(self, tmp_path):
        (tmp_path / "code.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
        r = _make_registry()
        ctx = _make_ctx(tmp_path, r)

        result = await r.execute_tool("bash", {"command": "tail -n +2 code.py"}, ctx)

        assert result.metadata.get("tool") == "read"
        assert result.metadata.get("routed_tool_args") == {"file_path": "code.py", "offset": 2}
        assert "1\tone" not in result.output
        assert "2\ttwo" in result.output
        assert "3\tthree" in result.output


# ---------------------------------------------------------------------------
# 7. bash search auto-route
# ---------------------------------------------------------------------------


class TestBashSearchAutoRoute:
    """bash search commands auto-route to grep/glob tools."""

    @pytest.mark.asyncio
    async def test_grep_routes_and_records_matching_line_coverage(self, tmp_path):
        target = tmp_path / "code.py"
        target.write_text("keep\nREMOVE_ME\n", encoding="utf-8")
        r = _make_registry()
        ctx = _make_ctx(tmp_path, r)

        grep_result = await r.execute_tool(
            "bash", {"command": "grep -n REMOVE_ME code.py"}, ctx
        )
        replace_result = await r.execute_tool(
            "replace",
            {
                "file_path": "code.py",
                "bounds": [{"line_no": 2, "anchor": "REMOVE_ME"}],
                "new_string": "",
            },
            ctx,
        )

        assert grep_result.metadata.get("tool") == "search"
        assert grep_result.metadata.get("routed_from") == "bash"
        assert grep_result.metadata.get("routed_tool_args") == {
            "query": "REMOVE_ME",
            "match": "regex",
            "case": "sensitive",
            "path": "code.py",
        }
        assert replace_result.metadata.get("error") is not True
        assert target.read_text(encoding="utf-8") == "keep\n"

    @pytest.mark.asyncio
    async def test_find_files_routes_to_glob(self, tmp_path):
        (tmp_path / "a.py").touch()
        (tmp_path / "notes.txt").touch()
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "b.py").touch()
        r = _make_registry()
        ctx = _make_ctx(tmp_path, r)

        result = await r.execute_tool(
            "bash", {"command": "find . -type f -name '*.py'"}, ctx
        )
        payload = json.loads(result.output)

        assert result.metadata.get("tool") == "find"
        assert result.metadata.get("routed_from") == "bash"
        assert result.metadata.get("routed_tool_args") == {"path": ".", "extensions": ["py"], "case": "sensitive"}
        assert payload["files"] == [
            {"path": "a.py", "name": "a.py"},
            {"path": "sub/b.py", "name": "b.py"},
        ]

    @pytest.mark.asyncio
    async def test_cd_prefixed_grep_runs_in_requested_directory(self, tmp_path):
        (tmp_path / "code.py").write_text("ROOT\n", encoding="utf-8")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "code.py").write_text("NEEDLE\n", encoding="utf-8")
        r = _make_registry()
        ctx = _make_ctx(tmp_path, r)

        result = await r.execute_tool(
            "bash", {"command": "cd sub && grep NEEDLE code.py"}, ctx
        )
        payload = json.loads(result.output)

        assert result.metadata.get("routed_from") is None
        assert payload["ok"] is True
        assert payload["stdout"] == "NEEDLE\n"


# ---------------------------------------------------------------------------
# 8. RouteHint tool_args for auto-routable commands
# ---------------------------------------------------------------------------


class TestRouteHintToolArgs:
    """Auto-routable hints carry structured tool arguments."""

    def test_cat_hint_tool_args(self):
        h = try_hint("cat file.py")
        assert h is not None
        assert h.tool_id == "read"
        assert h.tool_args == {"file_path": "file.py"}

    def test_head_hint_tool_args(self):
        h = try_hint("head -n 5 file.py")
        assert h is not None
        assert h.tool_id == "read"
        assert h.tool_args == {"file_path": "file.py", "limit": 5}

    def test_tail_plus_hint_tool_args(self):
        h = try_hint("tail -n +5 file.py")
        assert h is not None
        assert h.tool_id == "read"
        assert h.tool_args == {"file_path": "file.py", "offset": 5}

    def test_grep_hint_tool_args(self):
        h = try_hint("grep -r foo .")
        assert h is not None
        assert h.tool_id == "search"
        assert h.tool_args == {"query": "foo", "match": "regex", "case": "sensitive", "path": "."}

    def test_find_hint_tool_args(self):
        h = try_hint("find . -type f -name '*.py'")
        assert h is not None
        assert h.tool_id == "find"
        assert h.tool_args == {"path": ".", "extensions": ["py"], "case": "sensitive"}


# ---------------------------------------------------------------------------
# 8. bash git 含 -c 配置时降级
# ---------------------------------------------------------------------------


class TestBashGitConfigFlagFallback:
    """git -c key=value falls back to raw bash execution."""

    @pytest.mark.asyncio
    async def test_git_with_c_flag_runs_as_bash(self, tmp_path):
        r = _make_registry()
        ctx = _make_ctx(tmp_path, r)
        result = await r.execute_tool(
            "bash", {"command": "git -c core.autocrlf=true status"}, ctx
        )

        assert result.metadata.get("skipped") is not True
        assert "not a git repository" in result.output.lower()
        assert "routed_from" not in result.metadata


# ---------------------------------------------------------------------------
# 9. bash git --git-dir 不 hint
# ---------------------------------------------------------------------------


class TestBashGitGlobalDirNoHint:
    """git --git-dir=x does not produce a hint — runs as raw shell command."""

    @pytest.mark.asyncio
    async def test_git_dir_runs_as_shell(self, tmp_path):
        r = _make_registry()
        ctx = _make_ctx(tmp_path, r)
        result = await r.execute_tool(
            "bash", {"command": "git --git-dir=/nonexistent status"}, ctx
        )

        assert result.metadata.get("skipped") is not True
        assert "routed_from" not in result.metadata


# ---------------------------------------------------------------------------
# 10. filtered registry 降级
# ---------------------------------------------------------------------------


class TestBashFilteredRegistryFallback:
    """When registry excludes git, git runs as a raw bash command."""

    @pytest.mark.asyncio
    async def test_filtered_registry_runs_as_bash(self, tmp_path):
        r = _make_registry()
        r.filter_tools({"bash"})
        ctx = _make_ctx(tmp_path, r)
        result = await r.execute_tool("bash", {"command": "git status"}, ctx)

        assert result.metadata.get("skipped") is not True
        assert "not a git repository" in result.output.lower()
        assert "routed_from" not in result.metadata


# ---------------------------------------------------------------------------
# 11. try_hint 异常安全
# ---------------------------------------------------------------------------


class TestTryHintExceptionSafety:
    """try_hint catches all exceptions and returns None."""

    def test_exception_returns_none(self, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("parser crash")

        monkeypatch.setattr("voidx.tooling.builtin.shell.bash.router._try_hint_impl", boom)
        assert try_hint("git status") is None


# ---------------------------------------------------------------------------
# 6. powershell git auto-route (skipped on non-Windows)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="powershell tool only on Windows")
class TestPowerShellGitAutoRouteSkipped:
    """PowerShell auto-route is skipped on non-Windows platforms."""

    def test_powershell_not_registered_on_unix(self):
        r = _make_registry()
        assert r.get("powershell") is None


@pytest.mark.skipif(os.name != "nt", reason="powershell tool only on Windows")
class TestPowerShellGitAutoRoute:
    """PowerShell git auto-route on Windows."""

    @pytest.mark.asyncio
    async def test_powershell_git_routes(self, tmp_path):
        r = _make_registry()
        ctx = _make_ctx(tmp_path, r)
        result = await r.execute_tool("powershell", {"command": "git status"}, ctx)
        assert result.metadata.get("routed_from") == "powershell"


# ---------------------------------------------------------------------------
# Hidden git schema and bash fallback behavior
# ---------------------------------------------------------------------------


class TestBashGitHiddenFromLlm:
    def test_git_is_hidden_from_llm_but_registered_for_routing(self):
        from voidx.agent.domain.profile import RuntimeProfile
        from voidx.agent.adapters.langgraph.runtime.tool_surface import (
            ToolSurfaceContext,
            resolve_tool_surface,
        )

        r = _make_registry()
        surface = resolve_tool_surface(
            r,
            ToolSurfaceContext(
                runtime_profile=RuntimeProfile(profile_id="coding", revision=1, name="Coding"),
            ),
        )
        names = [tool["function"]["name"] for tool in surface.definitions]

        assert "git" not in names
        assert r.get("git") is not None

    @pytest.mark.asyncio
    async def test_git_hint_without_registry_falls_back_to_bash(self, tmp_path):
        r = _make_registry()
        ctx = _make_ctx(tmp_path, registry=None)

        result = await r.execute_tool("bash", {"command": "git status"}, ctx)

        assert result.metadata.get("skipped") is not True
        assert "not a git repository" in result.output.lower()
        assert "routed_from" not in result.metadata

    @pytest.mark.asyncio
    async def test_git_config_hint_falls_back_to_bash(self, tmp_path):
        r = _make_registry()
        ctx = _make_ctx(tmp_path, r)

        result = await r.execute_tool(
            "bash", {"command": "git -c advice.detachedHead=false status"}, ctx
        )

        assert result.metadata.get("skipped") is not True
        assert "not a git repository" in result.output.lower()
        assert "routed_from" not in result.metadata
