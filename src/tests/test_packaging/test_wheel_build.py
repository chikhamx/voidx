"""Verify voidx-cli wheel build artifacts are not corrupted.

Regression test for a bug where tui/build/lib/ (setuptools intermediate
output) was packaged into the wheel, producing paths like
build/lib/build/lib/.../voidx_cli/__init__.py instead of the correct
top-level voidx_cli/__init__.py. This made the published wheel
uninstallable — pip extracted files to site-packages/build/lib/... and
`import voidx_cli` failed with ModuleNotFoundError.
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
TUI_ROOT = ROOT / "tui"


def _has_build_module() -> bool:
    return subprocess.run(
        [sys.executable, "-c", "import build.__main__"],
        capture_output=True,
    ).returncode == 0


@pytest.mark.skipif(not _has_build_module(), reason="build module not installed")
def test_voidx_cli_wheel_has_no_build_lib_paths(tmp_path: Path) -> None:
    """Wheel must not contain build/lib/ paths from setuptools intermediate output."""
    result = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(tmp_path), str(TUI_ROOT)],
        cwd=TUI_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    wheels = list(tmp_path.glob("voidx_cli-*.whl"))
    assert len(wheels) == 1, f"expected one wheel, got {wheels}"
    wheel = wheels[0]

    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()

    bad = [n for n in names if n.startswith("build/lib/") or "/build/lib/" in n]
    assert not bad, f"wheel contains build/lib paths (corrupted): {bad[:5]}"


@pytest.mark.skipif(not _has_build_module(), reason="build module not installed")
def test_voidx_cli_wheel_excludes_tests_directory(tmp_path: Path) -> None:
    """Wheel must not include the tests/ directory."""
    result = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(tmp_path), str(TUI_ROOT)],
        cwd=TUI_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    wheels = list(tmp_path.glob("voidx_cli-*.whl"))
    assert len(wheels) == 1, f"expected one wheel, got {wheels}"
    wheel = wheels[0]

    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()

    test_files = [n for n in names if n.startswith("tests/") or n.startswith("build/lib/tests/")]
    assert not test_files, f"wheel includes tests/ directory: {test_files[:5]}"


@pytest.mark.skipif(not _has_build_module(), reason="build module not installed")
def test_voidx_cli_wheel_has_top_level_voidx_cli(tmp_path: Path) -> None:
    """Wheel must contain voidx_cli/ at the top level (not nested under build/lib)."""
    result = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(tmp_path), str(TUI_ROOT)],
        cwd=TUI_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    wheels = list(tmp_path.glob("voidx_cli-*.whl"))
    assert len(wheels) == 1, f"expected one wheel, got {wheels}"
    wheel = wheels[0]

    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()

    assert any(n == "voidx_cli/__init__.py" for n in names), \
        "wheel missing top-level voidx_cli/__init__.py"
