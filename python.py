#!/usr/bin/env python3
"""voidx Python launcher.

Prefers the validated runtime shared with the desktop app under ``~/.voidx``
and falls back to the legacy install venv for development and migration.

Usage:
  ./python.py -m pytest tests/ -v
  ./python.py scripts/package.py
  .\\python.py scripts\\package.py
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path


_FINGERPRINT_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_WORKSPACE_ROOT = Path(__file__).resolve().parent


def _default_voidx_home() -> Path:
    if platform.system() == "Windows":
        local_appdata = os.environ.get("LOCALAPPDATA")
        if not local_appdata:
            print("\n  ❌ LOCALAPPDATA is not set", file=sys.stderr)
            print("     Set VOIDX_HOME to your install directory.", file=sys.stderr)
            sys.exit(1)
        return Path(local_appdata) / "voidx"

    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home) / "voidx"
    return Path.home() / ".local" / "share" / "voidx"


def _default_data_root() -> Path:
    return Path(os.environ.get("VOIDX_DATA_ROOT") or (Path.home() / ".voidx"))


def _venv_python(voidx_home: Path) -> Path:
    if platform.system() == "Windows":
        return voidx_home / "venv" / "Scripts" / "python.exe"
    return voidx_home / "venv" / "bin" / "python"


def _safe_runtime_path(runtime_dir: Path, relative: object) -> Path | None:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        return None
    candidate = Path(relative)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        return None
    runtime_dir = runtime_dir.resolve()
    resolved = (runtime_dir / candidate).resolve()
    try:
        resolved.relative_to(runtime_dir)
    except ValueError:
        return None
    return resolved


def _current_runtime(data_root: Path) -> tuple[Path, Path] | None:
    current_path = data_root / "runtime" / "current.json"
    try:
        manifest = json.loads(current_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(manifest, dict):
        return None
    fingerprint = manifest.get("image_fingerprint")
    if not isinstance(fingerprint, str) or not _FINGERPRINT_RE.fullmatch(fingerprint):
        return None
    versions_root = (data_root / "runtime" / "versions").resolve()
    runtime_dir = (versions_root / fingerprint).resolve()
    try:
        runtime_dir.relative_to(versions_root)
    except ValueError:
        return None
    if not runtime_dir.is_dir():
        return None
    python = _safe_runtime_path(runtime_dir, manifest.get("python_relative"))
    site_packages = _safe_runtime_path(runtime_dir, manifest.get("site_packages_relative"))
    if python is None or site_packages is None or not python.is_file() or not site_packages.is_dir():
        return None
    return python, site_packages


def _runtime_env(site_packages: Path, data_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    paths = [str(_WORKSPACE_ROOT / "src"), str(_WORKSPACE_ROOT), str(site_packages)]
    existing = env.get("PYTHONPATH")
    if existing:
        paths.extend(
            entry
            for entry in existing.split(os.pathsep)
            if entry and entry not in paths
        )
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env["PYTHONSAFEPATH"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    env["VOIDX_HOME"] = str(data_root)
    return env


def _run(python: Path, site_packages: Path | None, args: list[str]) -> int:
    env = os.environ.copy()
    if site_packages is not None:
        env = _runtime_env(site_packages, _default_data_root())
    command = [str(python), *args]
    if platform.system() == "Windows":
        return subprocess.call(command, env=env)
    os.execve(str(python), command, env)
    return 0


def main() -> int:
    configured_home = os.environ.get("VOIDX_HOME")
    if configured_home:
        legacy_python = _venv_python(Path(configured_home))
        if legacy_python.exists():
            return _run(legacy_python, None, sys.argv[1:])

    runtime = _current_runtime(_default_data_root())
    if runtime is not None:
        return _run(runtime[0], runtime[1], sys.argv[1:])

    legacy_python = _venv_python(_default_voidx_home())
    if not legacy_python.exists():
        print(f"\n  ❌ voidx Python runtime not found at {legacy_python}", file=sys.stderr)
        print("     Install the desktop bundle or run scripts/install.sh.", file=sys.stderr)
        return 1
    return _run(legacy_python, None, sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
