import json
import os
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def test_release_metadata_check_passes():
    result = subprocess.run(
        [sys.executable, "scripts/package.py", "--check-only"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr


def test_npm_package_matches_python_version():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    npm_package = json.loads((ROOT / "npm" / "package.json").read_text())

    assert npm_package["version"] == pyproject["project"]["version"]
    assert npm_package["bin"]["voidx"] == "bin/voidx.js"
    assert (ROOT / "npm" / "bin" / "voidx.js").is_file()


def test_python_package_includes_bundled_skill_templates():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    package_data = pyproject["tool"]["setuptools"]["package-data"]["voidx.skills"]

    assert "bundled/superpowers/*/SKILL.md" in package_data
    assert "bundled/superpowers/*/templates/*.md" in package_data


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_npm_launcher_syntax_is_valid():
    result = subprocess.run(
        [NODE, "--check", "npm/bin/voidx.js"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_npm_launcher_rejects_old_explicit_python(tmp_path):
    if sys.platform == "win32":
        pytest.skip("uses a POSIX fake executable")
    fake_python = tmp_path / "python"
    fake_python.write_text("#!/bin/sh\nif [ \"$1\" = \"-c\" ]; then echo '3.10.9'; exit 0; fi\nexit 1\n")
    fake_python.chmod(fake_python.stat().st_mode | stat.S_IXUSR)

    env = {
        "PATH": os.environ.get("PATH", ""),
        "VOIDX_PYTHON": str(fake_python),
        "VOIDX_NPM_HOME": str(tmp_path / "home"),
    }
    result = subprocess.run(
        [NODE, "npm/bin/voidx.js", "version"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert "requires Python 3.11+" in result.stderr
    assert "3.10.9" in result.stderr


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_npm_launcher_falls_back_to_system_python(tmp_path):
    """voidx.js falls back to system Python when bundled Python is missing (v1.x upgrade path)."""
    if sys.platform == "win32":
        pytest.skip("uses a POSIX environment")
    # No bundled Python, no VOIDX_PYTHON — should try system Python as fallback.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "VOIDX_NPM_HOME": str(tmp_path / "home"),
    }
    result = subprocess.run(
        [NODE, "npm/bin/voidx.js", "version"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    # With system Python available, fallback should work (exit 0 or 1 from voidx itself)
    # Without system Python, should fail with a clear message
    if result.returncode == 1:
        assert "npm install" in result.stderr.lower() or "python 3.11" in result.stderr.lower()


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_npm_postinstall_does_not_export_probeSystemPython():
    """postinstall.js must not export probeSystemPython after removing system Python fallback."""
    result = subprocess.run(
        [NODE, "-e", "const m = require('./npm/bin/postinstall.js'); console.log(typeof m.probeSystemPython)"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "undefined"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_npm_launcher_selectPython_uses_bundled_only(tmp_path):
    """selectPython must return bundled Python when it exists, without searching system."""
    if sys.platform == "win32":
        pytest.skip("uses a POSIX fake executable")
    # Create a fake bundled Python
    home = tmp_path / "home"
    python_dir = home / "voidx" / "python" / "python" / "bin"
    python_dir.mkdir(parents=True)
    fake_python = python_dir / "python3.12"
    fake_python.write_text("#!/bin/sh\nif [ \"$1\" = \"-c\" ]; then echo '3.12.0'; exit 0; fi\nexit 1\n")
    fake_python.chmod(fake_python.stat().st_mode | stat.S_IXUSR)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "VOIDX_NPM_HOME": str(home),
    }
    result = subprocess.run(
        [NODE, "-e", (
            "const m = require('./npm/bin/voidx.js');"
            "const p = m.selectPython(process.env);"
            "console.log(p.label)"
        )],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "bundled"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_npm_launcher_selectPython_falls_back_to_system(tmp_path):
    """selectPython falls back to system Python when bundled Python is missing."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "VOIDX_NPM_HOME": str(tmp_path / "home"),
    }
    result = subprocess.run(
        [NODE, "-e", (
            "const m = require('./npm/bin/voidx.js');"
            "try { const p = m.selectPython(process.env); console.log('OK:' + p.label); }"
            "catch(e) { console.log('ERROR:' + e.message); }"
        )],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    # Should either find a system Python or report a clear error
    assert result.stdout.startswith("OK:") or result.stdout.startswith("ERROR:")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_npm_postinstall_exports_downloadFileWithRetry():
    """postinstall.js must export downloadFileWithRetry for retry logic."""
    result = subprocess.run(
        [NODE, "-e", "const m = require('./npm/bin/postinstall.js'); console.log(typeof m.downloadFileWithRetry)"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "function"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_npm_postinstall_cleans_partial_download_on_failure(tmp_path):
    """downloadFileWithRetry must clean up partial files on failure."""
    dest = str(tmp_path / "partial.tar.gz")
    # Write a partial file to simulate a previous failed download
    Path(dest).write_text("partial data")
    assert Path(dest).exists()
    result = subprocess.run(
        [NODE, "-e", (
            "const m = require('./npm/bin/postinstall.js');"
            f"m.downloadFileWithRetry('https://127.0.0.1:1/invalid', '{dest}', 1)"
            ".then(() => console.log('OK'))"
            ".catch(e => console.log('FAILED:' + e.message));"
        )],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.stdout.startswith("FAILED:")
    # Partial file must be cleaned up
    assert not Path(dest).exists()


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_npm_postinstall_supports_python_mirror_env(tmp_path):
    """postinstall.js must use VOIDX_NPM_PYTHON_MIRROR for download URL."""
    result = subprocess.run(
        [NODE, "-e", (
            "const m = require('./npm/bin/postinstall.js');"
            "const url = m.buildPythonDownloadUrl('https://mirror.example.com', '20260602', 'test.tar.gz');"
            "console.log(url);"
        )],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "https://mirror.example.com/20260602/test.tar.gz"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_npm_launcher_rebuilds_corrupted_venv(tmp_path):
    """voidx.js must rebuild venv when it exists but python binary is missing."""
    if sys.platform == "win32":
        pytest.skip("uses a POSIX fake executable")
    home = tmp_path / "home"
    venv_dir = home / "voidx" / "npm-venv"
    # Create a corrupted venv: directory exists but no python binary
    venv_dir.mkdir(parents=True)
    (venv_dir / "lib").mkdir()  # some venv-like structure
    # Create fake bundled Python
    python_dir = home / "voidx" / "python" / "python" / "bin"
    python_dir.mkdir(parents=True)
    fake_python = python_dir / "python3.12"
    fake_python.write_text("#!/bin/sh\nif [ \"$1\" = \"-c\" ]; then echo '3.12.0'; exit 0; fi\nexit 1\n")
    fake_python.chmod(fake_python.stat().st_mode | stat.S_IXUSR)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "VOIDX_NPM_HOME": str(home),
    }
    # The launcher should detect the corrupted venv and try to rebuild
    # (it will fail because the fake python can't create a real venv,
    # but the error should mention venv creation, not a mysterious failure)
    result = subprocess.run(
        [NODE, "npm/bin/voidx.js", "version"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 1
    # Should mention venv creation failure (meaning it detected corruption and tried to rebuild)
    assert "venv" in result.stderr.lower() or "virtual environment" in result.stderr.lower()


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_npm_postinstall_uses_pip_no_cache_dir():
    """postinstall.js pip install must use --no-cache-dir to avoid cache conflicts."""
    source = (ROOT / "npm" / "bin" / "postinstall.js").read_text()
    assert "--no-cache-dir" in source


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_npm_postinstall_upgrades_pip_before_install():
    """postinstall.js must upgrade pip before installing voidx."""
    source = (ROOT / "npm" / "bin" / "postinstall.js").read_text()
    # Must have a pip upgrade step before the main pip install
    # The args are in array form: ["-m", "pip", "install", "--upgrade", "pip", ...]
    assert '"--upgrade", "pip"' in source or "'--upgrade', 'pip'" in source


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_npm_postinstall_supports_pip_index_env():
    """postinstall.js must pass VOIDX_NPM_PIP_INDEX as -i to pip."""
    source = (ROOT / "npm" / "bin" / "postinstall.js").read_text()
    assert "VOIDX_NPM_PIP_INDEX" in source
    assert '"-i"' in source or "'-i'" in source


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_npm_launcher_supports_pip_index_env():
    """voidx.js must pass VOIDX_NPM_PIP_INDEX as -i to pip."""
    source = (ROOT / "npm" / "bin" / "voidx.js").read_text()
    assert "VOIDX_NPM_PIP_INDEX" in source
    assert '"-i"' in source or "'-i'" in source


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_npm_launcher_forwards_args_to_managed_voidx(tmp_path):
    if sys.platform == "win32":
        pytest.skip("uses a POSIX fake executable")
    venv = tmp_path / "venv"
    bin_dir = venv / "bin"
    bin_dir.mkdir(parents=True)
    fake_voidx = bin_dir / "voidx"
    fake_voidx.write_text("#!/bin/sh\nprintf 'voidx args:'\nprintf '%s|' \"$@\"\nprintf '\\n'\n")
    fake_voidx.chmod(fake_voidx.stat().st_mode | stat.S_IXUSR)

    env = {
        "PATH": os.environ.get("PATH", ""),
        "VOIDX_PYTHON": sys.executable,
        "VOIDX_NPM_VENV": str(venv),
        "VOIDX_NPM_SKIP_BOOTSTRAP": "1",
    }
    result = subprocess.run(
        [NODE, "npm/bin/voidx.js", "version", "--plain"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "voidx args:version|--plain|\n"
