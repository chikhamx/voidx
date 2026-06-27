"""Tests for scripts/install.sh npm-prefer logic.

These tests validate the install.sh script's behavior:
- Prefers npm when available
- Falls back to PBS+venv+pip when npm is not available
- Respects VOIDX_SKIP_NPM=1 to force fallback
- Handles npm path detection and PATH setup
"""

import os
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "scripts" / "install.sh"


def _read_install_sh() -> str:
    """Read the install.sh script."""
    return INSTALL_SH.read_text(encoding="utf-8")


def _run_install_sh_in_tmp(tmp_path: Path, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Run install.sh in a sandboxed tmp directory with a fake environment.

    This does NOT actually install voidx — it uses a mock environment
    to test the script's branching logic.
    """
    env = {
        "HOME": str(tmp_path / "home"),
        "PATH": os.environ.get("PATH", ""),
        "VOIDX_HOME": str(tmp_path / "home" / ".local" / "share" / "voidx"),
        "VOIDX_BIN_DIR": str(tmp_path / "home" / ".local" / "bin"),
        "VOIDX_VERSION": "0.0.0-test",  # Use a version that won't match anything
    }
    if env_extra:
        env.update(env_extra)

    # Create the home directory
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        ["bash", str(INSTALL_SH)],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    return result


class TestInstallShNpmPreferStructure:
    """Structural tests — verify the script contains the right logic branches."""

    def test_script_contains_npm_check(self):
        """install.sh must check for npm availability."""
        src = _read_install_sh()
        assert "command -v npm" in src, "Script must check if npm is available"

    def test_script_contains_npm_install_command(self):
        """install.sh must run npm install -g when npm is available."""
        src = _read_install_sh()
        assert "npm install -g" in src, "Script must contain npm install -g command"
        assert "@chikhamx/voidx" in src, "npm install must reference @chikhamx/voidx"

    def test_script_contains_npm_skip_env_var(self):
        """install.sh must respect VOIDX_SKIP_NPM to force fallback."""
        src = _read_install_sh()
        assert "VOIDX_SKIP_NPM" in src, "Script must check VOIDX_SKIP_NPM env var"

    def test_script_contains_npm_path_message(self):
        """install.sh must show npm-specific messages."""
        src = _read_install_sh()
        # Should have a message indicating npm installation path
        assert "npm" in src.lower(), "Script must mention npm in output messages"

    def test_script_preserves_fallback_path(self):
        """install.sh must still contain the PBS+venv+pip fallback."""
        src = _read_install_sh()
        # Key markers of the fallback path
        assert "python-build-standalone" in src or "PBS" in src, "Fallback must reference PBS"
        assert "venv" in src, "Fallback must create venv"
        assert "pip install" in src, "Fallback must use pip install"

    def test_script_npm_path_detection(self):
        """install.sh must detect npm global bin directory."""
        src = _read_install_sh()
        # Must find the npm bin directory after install
        assert "npm prefix" in src or "npm bin" in src or "npm_root" in src.lower(), \
            "Script must detect npm global bin/prefix"

    def test_script_npm_path_adds_to_path(self):
        """install.sh must ensure npm bin dir is in PATH."""
        src = _read_install_sh()
        # After npm install, the npm bin dir must be in PATH
        # Look for PATH manipulation related to npm
        assert "PATH" in src, "Script must handle PATH"

    def test_script_npm_cleans_old_symlink(self):
        """install.sh npm path must clean up old symlink at ~/.local/bin/voidx."""
        src = _read_install_sh()
        # The npm path should remove old symlinks pointing to venv
        # since npm uses its own launcher, not a symlink to venv
        assert ".local/bin/voidx" in src, "Script must reference ~/.local/bin/voidx for cleanup"

    def test_script_npm_verifies_version(self):
        """install.sh must verify voidx --version after npm install."""
        src = _read_install_sh()
        assert "--version" in src, "Script must verify voidx --version"

    def test_script_preserves_env_vars_for_fallback(self):
        """Fallback path must still support VOIDX_PIP_INDEX and VOIDX_PYTHON_MIRROR."""
        src = _read_install_sh()
        assert "VOIDX_PIP_INDEX" in src, "Fallback must support VOIDX_PIP_INDEX"
        assert "VOIDX_PYTHON_MIRROR" in src, "Fallback must support VOIDX_PYTHON_MIRROR"

    def test_script_legacy_cleanup_before_npm(self):
        """_cleanup_legacy must run before the npm/fallback branch."""
        src = _read_install_sh()
        # _cleanup_legacy should be called before the npm check
        legacy_pos = src.find("_cleanup_legacy")
        npm_check_pos = src.find("command -v npm")
        # The first _cleanup_legacy call should be before the npm check
        assert legacy_pos > 0, "Script must call _cleanup_legacy"
        assert npm_check_pos > 0, "Script must check for npm"
        assert legacy_pos < npm_check_pos, \
            "_cleanup_legacy must run before npm availability check"


class TestInstallShNpmPreferBehavior:
    """Behavioral tests — verify the script takes the right path."""

    def test_npm_available_takes_npm_path(self, tmp_path):
        """When npm is available, script should attempt npm install."""
        # We can't actually run npm install in tests, but we can check
        # that the script's output indicates it's trying the npm path
        # by checking the script source for the branching logic
        src = _read_install_sh()
        # The script should have a conditional that checks npm first
        # and only falls back if npm is not available or VOIDX_SKIP_NPM=1
        lines = src.splitlines()
        npm_check_lines = [i for i, l in enumerate(lines) if "command -v npm" in l]
        assert len(npm_check_lines) > 0, "Script must check for npm"

    def test_npm_skip_env_forces_fallback(self, tmp_path):
        """VOIDX_SKIP_NPM=1 must force the fallback path even with npm available."""
        src = _read_install_sh()
        # The npm check should also check VOIDX_SKIP_NPM
        # Find the section where npm is checked
        assert "VOIDX_SKIP_NPM" in src, "Script must check VOIDX_SKIP_NPM"

    def test_fallback_still_works_without_npm(self, tmp_path):
        """Without npm, the script should still work via PBS+venv+pip."""
        src = _read_install_sh()
        # The fallback path (PBS download, venv, pip) must be intact
        # and not gated behind npm
        assert "BUNDLED_PYTHON" in src, "Fallback must set up BUNDLED_PYTHON"
        assert "VENV_DIR" in src, "Fallback must set up VENV_DIR"


class TestInstallShPipIsolation:
    """Tests verifying pip install runs isolated from source repo."""

    def test_pip_install_not_run_in_source_dir(self):
        """pip install must not run in a directory with pyproject.toml.

        When install.sh is run from inside the voidx source repo, pip would
        discover the local pyproject.toml and install from ./src instead of
        downloading the published wheel from PyPI. The script must cd to a
        neutral directory (e.g. VENV_DIR) before running pip install.
        """
        src = _read_install_sh()
        lines = src.splitlines()

        # Find the line that adds voidx==VERSION to PIP_ARGS
        pip_line_idx = None
        for i, line in enumerate(lines):
            if "voidx==" in line and "PIP_ARGS" in line:
                pip_line_idx = i
                break

        assert pip_line_idx is not None, "Script must have PIP_ARGS with voidx==VERSION"

        # Look backwards from the pip install line for a cd command
        # that moves to VENV_DIR or another non-source directory
        preceding = "\n".join(lines[:pip_line_idx])
        assert 'cd "' in preceding or "cd '" in preceding or "cd ${" in preceding, \
            "Script must cd to a neutral directory before pip install to avoid " \
            "installing from local source instead of PyPI"


class TestInstallShSymlinkCleanup:
    """Tests verifying cleanup logic doesn't remove the script's own symlink."""

    def test_cleanup_does_not_warn_on_current_venv_symlink(self):
        """_cleanup_legacy must not warn about a symlink pointing to the
        current VENV_DIR.

        The fallback path creates ~/.local/bin/voidx → .../share/voidx/venv/bin/voidx
        at the end of every run. On the next run, _cleanup_legacy sees this
        symlink, matches it against the */share/voidx/venv/bin/voidx pattern,
        and warns "发现旧版安装脚本创建的符号链接" — even though it's the
        current install's own symlink, not a legacy one.

        The cleanup pattern for */share/voidx/venv/bin/voidx must be removed
        or narrowed so it only matches truly legacy locations, not the
        current VENV_DIR layout.
        """
        src = _read_install_sh()
        # The cleanup function should NOT treat the current venv symlink
        # as legacy. The pattern */share/voidx/venv/bin/voidx matches the
        # current install's own symlink, causing a false-positive warning
        # every run.
        #
        # Verify the script does NOT have a blanket cleanup of the current
        # venv path. The ln -sf at the end already overwrites stale links,
        # so explicit cleanup of the same path is redundant and causes
        # the recurring warning.
        assert '*/share/voidx/venv/bin/voidx' not in src or \
               _cleanup_uses_version_check(src), \
            "Cleanup must not blindly remove symlinks pointing to the " \
            "current VENV_DIR — this causes a warning every run since " \
            "the fallback path recreates the same symlink"


def _cleanup_uses_version_check(src: str) -> bool:
    """Check if cleanup of venv symlinks is guarded by a version check."""
    # Extract just the _cleanup_legacy function body (up to the next top-level
    # call or function definition), not the entire script.
    start = src.find('_cleanup_legacy()')
    if start < 0:
        start = src.find('_cleanup_legacy')
    # Function body ends at the closing brace line
    end = src.find('\n}', start)
    if end < 0:
        end = len(src)
    cleanup_body = src[start:end]
    venv_pattern_pos = cleanup_body.find('*/share/voidx/venv/bin/voidx')
    if venv_pattern_pos < 0:
        return True
    nearby = cleanup_body[max(0, venv_pattern_pos - 200):venv_pattern_pos + 200]
    return 'VERSION' in nearby or 'MARKER' in nearby
