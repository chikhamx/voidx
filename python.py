#!/usr/bin/env python3
"""voidx Python launcher.

Locates the venv Python under VOIDX_HOME and forwards all arguments.

Usage:
  ./python.py -m pytest tests/ -v
  ./python.py scripts/package.py
  .\\python.py scripts\\package.py

Environment:
  VOIDX_HOME — install directory
    Linux/macOS default: ${XDG_DATA_HOME:-$HOME/.local/share}/voidx
    Windows default: %LOCALAPPDATA%\\voidx
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path


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


def _venv_python(voidx_home: Path) -> Path:
    if platform.system() == "Windows":
        return voidx_home / "venv" / "Scripts" / "python.exe"
    return voidx_home / "venv" / "bin" / "python"


def main() -> int:
    voidx_home = Path(os.environ.get("VOIDX_HOME") or _default_voidx_home())
    python = _venv_python(voidx_home)

    if not python.exists():
        print(f"\n  ❌ voidx venv Python not found at {python}", file=sys.stderr)
        print("     Run scripts/install.sh or scripts/install.ps1 to create it, or set VOIDX_HOME.", file=sys.stderr)
        return 1

    args = [str(python), *sys.argv[1:]]
    if platform.system() == "Windows":
        return subprocess.call(args)

    os.execv(str(python), args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
