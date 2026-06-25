"""Tests for resolve_safe — path resolution and sandbox boundary enforcement."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from voidx.tools.base import resolve_safe


class TestResolveSafeTildePath:
    """resolve_safe should expand ~ to the user's home directory."""

    def test_tilde_path_resolves_to_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        extra = str(tmp_path)
        result = resolve_safe(str(tmp_path), "~/secret.txt", [extra])
        assert result == (tmp_path / "secret.txt").resolve()

    def test_tilde_path_blocked_when_outside_workspace_and_extra(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        result = resolve_safe(str(workspace), "~/secret.txt", [])
        assert result is None

    def test_absolute_path_outside_workspace_resolves_directly(self, tmp_path):
        external = tmp_path / "external"
        external.mkdir()
        target = external / "file.txt"
        target.write_text("data")
        result = resolve_safe(str(tmp_path / "workspace"), str(target), [str(external)])
        assert result == target.resolve()

    def test_relative_path_still_joins_workspace(self, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "file.txt").write_text("data")
        result = resolve_safe(str(workspace), "file.txt", [])
        assert result == (workspace / "file.txt").resolve()

    def test_relative_path_traversal_blocked(self, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        result = resolve_safe(str(workspace), "../escape.txt", [])
        assert result is None


class TestAddExtraPathCallback:
    """ToolContext should accept and invoke an add_extra_path callback."""

    def test_callback_invoked_with_path(self, tmp_path):
        from voidx.tools.base import ToolContext

        captured: list[str] = []
        ctx = ToolContext(
            workspace=str(tmp_path),
            add_extra_path=captured.append,
        )
        ctx.add_extra_path("/some/external/dir")
        assert captured == ["/some/external/dir"]

    def test_callback_defaults_to_none(self, tmp_path):
        from voidx.tools.base import ToolContext

        ctx = ToolContext(workspace=str(tmp_path))
        assert ctx.add_extra_path is None
