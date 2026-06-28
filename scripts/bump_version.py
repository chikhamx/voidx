#!/usr/bin/env python3
"""Bump the voidx version across all files that carry a static version string.

The canonical source is ``src/voidx/__init__.py`` (``__version__``). This script
writes that file plus the three files that must hold a static copy:
``npm/package.json``, ``scripts/install.sh``, ``scripts/install.ps1``.

Usage:
    ./python.sh scripts/bump_version.py 3.4.0
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

INIT_PY = ROOT / "src" / "voidx" / "__init__.py"
NPM_PACKAGE = ROOT / "npm" / "package.json"
INSTALL_SH = ROOT / "scripts" / "install.sh"
INSTALL_PS1 = ROOT / "scripts" / "install.ps1"

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
INIT_VERSION_RE = re.compile(r'(__version__\s*=\s*")[^"]+(")')
INSTALL_SH_RE = re.compile(r'(VERSION="\$\{VOIDX_VERSION:-)\d+\.\d+\.\d+(\}")')
INSTALL_PS1_RE = re.compile(r'(\$Version = if \(\$env:VOIDX_VERSION\) \{ \$env:VOIDX_VERSION \} else \{ ")\d+\.\d+\.\d+(" \})')


def die(msg: str) -> None:
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def read_init_version() -> str:
    text = INIT_PY.read_text()
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    return m.group(1) if m else ""


def bump_init(version: str) -> str:
    text = INIT_PY.read_text()
    new, n = INIT_VERSION_RE.subn(rf'\g<1>{version}\g<2>', text)
    if n != 1:
        die(f"Expected 1 __version__ match in {INIT_PY}, found {n}.")
    return new


def bump_npm(version: str) -> str:
    data = json.loads(NPM_PACKAGE.read_text())
    data["version"] = version
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def bump_install_sh(version: str) -> str:
    text = INSTALL_SH.read_text()
    new, n = INSTALL_SH_RE.subn(rf'\g<1>{version}\g<2>', text)
    if n != 1:
        die(f"Expected 1 VERSION match in {INSTALL_SH}, found {n}.")
    return new


def bump_install_ps1(version: str) -> str:
    text = INSTALL_PS1.read_text()
    new, n = INSTALL_PS1_RE.subn(rf'\g<1>{version}\g<2>', text)
    if n != 1:
        die(f"Expected 1 $Version match in {INSTALL_PS1}, found {n}.")
    return new


def verify(version: str, contents: dict[Path, str]) -> None:
    init_match = re.search(r'__version__\s*=\s*"([^"]+)"', contents[INIT_PY])
    init_version = init_match.group(1) if init_match else ""
    if init_version != version:
        die(f"{INIT_PY} has __version__={init_version!r}, expected {version!r}.")

    npm_version = json.loads(contents[NPM_PACKAGE]).get("version", "")
    if npm_version != version:
        die(f"{NPM_PACKAGE} has version={npm_version!r}, expected {version!r}.")

    sh_match = INSTALL_SH_RE.search(contents[INSTALL_SH])
    if not sh_match or sh_match.group(0).count(version) != 1:
        die(f"{INSTALL_SH} does not contain version {version}.")

    ps1_match = INSTALL_PS1_RE.search(contents[INSTALL_PS1])
    if not ps1_match or ps1_match.group(0).count(version) != 1:
        die(f"{INSTALL_PS1} does not contain version {version}.")


def main() -> None:
    if len(sys.argv) != 2:
        die(f"Usage: {sys.argv[0]} <version>")

    version = sys.argv[1]
    if not SEMVER_RE.match(version):
        die(f"Version {version!r} is not a valid semver (X.Y.Z).")

    old = read_init_version()
    if old == version:
        print(f"Version is already {version}, nothing to do.")
        return

    print(f"Bumping version: {old} → {version}")

    contents = {
        INIT_PY: bump_init(version),
        NPM_PACKAGE: bump_npm(version),
        INSTALL_SH: bump_install_sh(version),
        INSTALL_PS1: bump_install_ps1(version),
    }

    verify(version, contents)

    for path, text in contents.items():
        path.write_text(text)
        print(f"  ✓ {path.relative_to(ROOT)}")

    print(f"Version bumped to {version}. Run preflight before publishing.")


if __name__ == "__main__":
    main()
