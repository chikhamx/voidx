"""Tests for scripts/release.py _sync_npm_wheel.

Regression guard for the 3.7.0 incident where manual ``npm publish``
shipped a stale 3.6.0 wheel because the wheel-sync step was skipped.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import release  # type: ignore[import-not-found]


def _make_wheel(directory: Path, name: str) -> Path:
    """Create a minimal fake wheel file inside directory."""
    wheel = directory / name
    wheel.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    return wheel
    """Create a minimal fake wheel file."""
    path.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    return path / name


def test_sync_npm_wheel_removes_stale_and_copies_new(tmp_path: Path) -> None:
    """_sync_npm_wheel must delete old wheels and copy the fresh one from dist/."""
    dist_dir = tmp_path / "dist"
    npm_dir = tmp_path / "npm"
    dist_dir.mkdir()
    npm_dir.mkdir()

    # Stale wheel from previous version
    stale = _make_wheel(npm_dir, "voidx_cli-3.6.0-py3-none-any.whl")
    assert stale.exists()

    # Fresh wheel in dist/
    fresh = _make_wheel(dist_dir, "voidx_cli-3.7.1-py3-none-any.whl")

    result = release._sync_npm_wheel(dist_dir=dist_dir, npm_dir=npm_dir)

    assert result == npm_dir / fresh.name
    assert not stale.exists(), "stale wheel was not removed"
    assert (npm_dir / fresh.name).exists(), "fresh wheel was not copied"


def test_sync_npm_wheel_no_multiple_wheels(tmp_path: Path) -> None:
    """After sync, npm/ must contain exactly one wheel matching dist/."""
    dist_dir = tmp_path / "dist"
    npm_dir = tmp_path / "npm"
    dist_dir.mkdir()
    npm_dir.mkdir()

    # Multiple stale wheels in npm/
    _make_wheel(npm_dir, "voidx_cli-3.5.0-py3-none-any.whl")
    _make_wheel(npm_dir, "voidx_cli-3.6.0-py3-none-any.whl")

    _make_wheel(dist_dir, "voidx_cli-3.7.1-py3-none-any.whl")

    release._sync_npm_wheel(dist_dir=dist_dir, npm_dir=npm_dir)

    wheels = list(npm_dir.glob("voidx_cli-*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"
    assert wheels[0].name == "voidx_cli-3.7.1-py3-none-any.whl"


def test_sync_npm_wheel_returns_none_when_dist_empty(tmp_path: Path) -> None:
    """_sync_npm_wheel must return None when dist/ has no wheel."""
    dist_dir = tmp_path / "dist"
    npm_dir = tmp_path / "npm"
    dist_dir.mkdir()
    npm_dir.mkdir()

    result = release._sync_npm_wheel(dist_dir=dist_dir, npm_dir=npm_dir)
    assert result is None


def test_sync_npm_wheel_preserves_stale_removal_when_dist_empty(tmp_path: Path) -> None:
    """Even when dist/ is empty, stale wheels in npm/ must be removed."""
    dist_dir = tmp_path / "dist"
    npm_dir = tmp_path / "npm"
    dist_dir.mkdir()
    npm_dir.mkdir()

    stale = _make_wheel(npm_dir, "voidx_cli-3.6.0-py3-none-any.whl")

    release._sync_npm_wheel(dist_dir=dist_dir, npm_dir=npm_dir)

    assert not stale.exists(), "stale wheel should be removed even when dist is empty"
