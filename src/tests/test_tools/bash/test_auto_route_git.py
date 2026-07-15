"""Tests for bash/powershell auto-route to git tool via maybe_route_hint."""

from __future__ import annotations

import json
import os

import pytest

from voidx.tools.base import ToolContext
from voidx.tools.registry import ToolRegistry
from voidx.tools.shell.common import RouteHint
from voidx.tools.bash.router import try_hint


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(tmp_path, registry: ToolRegistry | None = None) -> ToolContext:
    return ToolContext(
        workspace=str(tmp_path),
        permission_mode="full_access",
        tool_registry=registry,
    )


def _make_registry() -> ToolRegistry:
    return ToolRegistry()


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
    """When ctx.tool_registry is None, falls back to hint-only result."""

    @pytest.mark.asyncio
    async def test_no_registry_returns_hint(self, tmp_path):
        r = _make_registry()
        ctx = _make_ctx(tmp_path, registry=None)
        result = await r.execute_tool("bash", {"command": "git status"}, ctx)

        assert result.metadata.get("skipped") is True
        assert result.metadata.get("route_hint", {}).get("tool_id") == "git"
        assert "routed_from" not in result.metadata


# ---------------------------------------------------------------------------
# 7. RouteHint tool_args 默认 None（非 git hint）
# ---------------------------------------------------------------------------


class TestRouteHintToolArgsDefaultNone:
    """Non-git hints keep tool_args=None and preserve old hint-only behavior."""

    def test_cat_hint_tool_args_none(self):
        h = try_hint("cat file.py")
        assert h is not None
        assert h.tool_id == "read"
        assert h.tool_args is None

    def test_grep_hint_tool_args_none(self):
        h = try_hint("grep -r foo .")
        assert h is not None
        assert h.tool_id == "grep"
        assert h.tool_args is None

    def test_find_hint_tool_args_none(self):
        h = try_hint("find . -name '*.py'")
        assert h is not None
        assert h.tool_id == "glob"
        assert h.tool_args is None


# ---------------------------------------------------------------------------
# 8. bash git 含 -c 配置时降级
# ---------------------------------------------------------------------------


class TestBashGitConfigFlagFallback:
    """git -c key=value falls back to hint-only (tool_args=None)."""

    @pytest.mark.asyncio
    async def test_git_with_c_flag_returns_hint(self, tmp_path):
        r = _make_registry()
        ctx = _make_ctx(tmp_path, r)
        result = await r.execute_tool(
            "bash", {"command": "git -c core.autocrlf=true status"}, ctx
        )

        assert result.metadata.get("skipped") is True
        assert result.metadata.get("route_hint", {}).get("tool_id") == "git"
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
    """When registry excludes git tool, falls back to hint-only result."""

    @pytest.mark.asyncio
    async def test_filtered_registry_returns_hint(self, tmp_path):
        r = _make_registry()
        r.filter_tools({"bash"})
        ctx = _make_ctx(tmp_path, r)
        result = await r.execute_tool("bash", {"command": "git status"}, ctx)

        assert result.metadata.get("skipped") is True
        assert result.metadata.get("route_hint", {}).get("tool_id") == "git"
        assert "routed_from" not in result.metadata


# ---------------------------------------------------------------------------
# 11. try_hint 异常安全
# ---------------------------------------------------------------------------


class TestTryHintExceptionSafety:
    """try_hint catches all exceptions and returns None."""

    def test_exception_returns_none(self, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("parser crash")

        monkeypatch.setattr("voidx.tools.bash.router._try_hint_impl", boom)
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
