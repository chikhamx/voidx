#!/usr/bin/env python3
"""voidx — unified cross-platform entry point.

Auto-detects the voidx venv Python and re-exec's into it (same process),
then sets up PYTHONPATH and runs voidx.main directly.

Usage:
  ./voidx.py <args>
  python voidx.py <args>
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path


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


HERE = Path(__file__).resolve().parent

# ── Re-exec into the voidx venv Python if not already running it ──────────
voidx_home = Path(os.environ.get("VOIDX_HOME") or _default_voidx_home())
venv_python = _venv_python(voidx_home)
current_python = Path(sys.executable).resolve() if sys.executable else None

if current_python and current_python != venv_python.resolve():
    if not venv_python.exists():
        print(f"\n  ❌ voidx venv Python not found at {venv_python}", file=sys.stderr)
        print("     Run scripts/install.sh or scripts/install.ps1 to create it,", file=sys.stderr)
        print("     or set VOIDX_HOME to your install directory.", file=sys.stderr)
        sys.exit(1)

    args = [str(venv_python), __file__, *sys.argv[1:]]
    if platform.system() == "Windows":
        sys.exit(subprocess.call(args))
    os.execv(str(venv_python), args)
    # os.execv does not return — but just in case:
    sys.exit(1)

# ── Now running under the correct venv Python ─────────────────────────────

# Remove script dir and cwd from sys.path so voidx.py doesn't shadow
# the src/voidx/ package directory.
script_dir = str(HERE)
for entry in (script_dir, ""):
    while entry in sys.path:
        sys.path.remove(entry)

# Ensure src/ and tui/ are on sys.path so voidx package is importable
for subdir in ("src", "tui"):
    path = str(HERE / subdir)
    if path not in sys.path:
        sys.path.insert(0, path)

# Also set PYTHONPATH env var for any child processes voidx spawns
pythonpath_parts = [str(HERE / "src"), str(HERE / "tui")]
existing = os.environ.get("PYTHONPATH")
if existing:
    pythonpath_parts.append(existing)
os.environ["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)

# Run the CLI entry point directly in this process
from voidx.main import cli  # noqa: E402

sys.exit(cli())
