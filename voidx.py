#!/usr/bin/env python3
"""voidx — unified cross-platform entry point.

Auto-detects the voidx runtime and re-exec's into its Python interpreter,
then sets up PYTHONPATH and runs voidx.main directly.

Usage:
  ./voidx.py <args>
  python voidx.py <args>
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


def _default_voidx_home() -> Path:
    """Locate VOIDX_HOME when the env var is not set."""
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


def _venv_python(voidx_home: Path) -> Path:
    """The Python interpreter inside the voidx venv."""
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


HERE = Path(__file__).resolve().parent
SOURCE_PATHS = [str(HERE / "src"), str(HERE / "tui")]


def _runtime_env(site_packages: Path, data_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    paths = [*SOURCE_PATHS, str(site_packages)]
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


# Ensure the re-executed interpreter sees the working tree before any installed copy.
existing_pythonpath = [
    entry
    for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep)
    if entry and entry not in SOURCE_PATHS
]
os.environ["PYTHONPATH"] = os.pathsep.join([*SOURCE_PATHS, *existing_pythonpath])

# ── Re-exec into the voidx runtime Python if not already running it ────────
voidx_home = Path(os.environ.get("VOIDX_HOME") or _default_voidx_home())
runtime = _current_runtime(voidx_home)
site_packages = runtime[1] if runtime is not None else None
python = runtime[0] if runtime is not None else _venv_python(voidx_home)
if site_packages is not None:
    os.environ.update(_runtime_env(site_packages, voidx_home))

current_python = Path(sys.executable).resolve() if sys.executable else None
if current_python and current_python != python.resolve():
    if not python.exists():
        print(f"\n  ❌ voidx Python runtime not found at {python}", file=sys.stderr)
        print("     Run scripts/install.sh or scripts/install.ps1 to create it,", file=sys.stderr)
        print("     or set VOIDX_HOME to your install directory.", file=sys.stderr)
        sys.exit(1)

    args = [str(python), __file__, *sys.argv[1:]]
    if platform.system() == "Windows":
        sys.exit(subprocess.call(args, env=os.environ.copy()))
    os.execve(str(python), args, os.environ.copy())
    sys.exit(1)

# ── Now running under the selected voidx Python ───────────────────────────

# Remove script dir and cwd so voidx.py cannot shadow the src/voidx package.
script_dir = str(HERE)
for entry in (script_dir, ""):
    while entry in sys.path:
        sys.path.remove(entry)

# Existing .pth or PYTHONPATH entries may place these directories after site-packages.
runtime_paths = [*SOURCE_PATHS]
if site_packages is not None:
    runtime_paths.append(str(site_packages))
for path in runtime_paths:
    while path in sys.path:
        sys.path.remove(path)
sys.path[:0] = runtime_paths

from voidx.main import cli  # noqa: E402

sys.exit(cli())
