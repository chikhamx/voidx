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
