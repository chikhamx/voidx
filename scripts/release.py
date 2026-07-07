#!/usr/bin/env python3
"""Release voidx to PyPI and npm. PyPI goes first, npm only if PyPI succeeds."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
NPM_DIR = ROOT / "npm"


def main() -> int:
    parser = argparse.ArgumentParser(description="Release voidx to PyPI and npm.")
    parser.add_argument("--pypi-only", action="store_true", help="Only publish to PyPI.")
    parser.add_argument("--npm-only", action="store_true", help="Only publish to npm.")
    parser.add_argument("--dry-run", action="store_true", help="Build but do not publish.")
    parser.add_argument("--skip-checks", action="store_true", help="Skip release metadata checks.")
    args = parser.parse_args()

    # 1. Sync npm version from pyproject.toml (before metadata check)
    pyproject_version = _get_pyproject_version()
    _sync_npm_version(pyproject_version)

    # 2. Metadata checks
    if not args.skip_checks:
        print("🔍 Checking release metadata...")
        result = _run([sys.executable, str(ROOT / "scripts" / "package.py"), "--check-only"])
        if result != 0:
            print("❌ Metadata check failed. Fix issues before releasing.")
            return result
        print("   ✅ Metadata OK")

    print(f"📦 Version: {pyproject_version}")

    publish_pypi = not args.npm_only
    publish_npm = not args.pypi_only
    exit_code = 0

    if publish_pypi:
        exit_code = _release_pypi(args.dry_run)
        if exit_code != 0:
            return exit_code

    if publish_npm:
        exit_code = _release_npm(args.dry_run)
        if exit_code != 0:
            return exit_code

    print("\n🎉 Release complete!")
    return 0


def _get_pyproject_version() -> str:
    init_text = (ROOT / "src" / "voidx" / "__init__.py").read_text()
    match = re.search(r'__version__\s*=\s*"([^"]+)"', init_text)
    if not match:
        raise RuntimeError("src/voidx/__init__.py is missing __version__.")
    return match.group(1)


def _sync_npm_version(version: str) -> None:
    npm_package = NPM_DIR / "package.json"
    data = json.loads(npm_package.read_text())
    if data.get("version") != version:
        data["version"] = version
        npm_package.write_text(json.dumps(data, indent=2) + "\n")
        print(f"   Synced npm/package.json version → {version}")


def _release_pypi(dry_run: bool) -> int:
    print("\n🐍 Building PyPI artifacts...")

    # Clean and build
    if DIST.exists():
        shutil.rmtree(DIST)
    result = _run([sys.executable, str(ROOT / "scripts" / "package.py"), "--format", "all", "--clean"])
    if result != 0:
        print("❌ PyPI build failed.")
        return result

    wheel = list(DIST.glob("*.whl"))
    sdist = list(DIST.glob("*.tar.gz"))
    print(f"   ✅ wheel:  {wheel[0].name if wheel else 'N/A'}")
    print(f"   ✅ sdist:  {sdist[0].name if sdist else 'N/A'}")

    if dry_run:
        print("   🏷️  Dry run — skipping PyPI upload.")
        return 0

    print("📤 Uploading to PyPI...")
    artifacts = sorted(DIST.glob("*.whl")) + sorted(DIST.glob("*.tar.gz"))
    if not artifacts:
        print("❌ No artifacts found in dist/.", file=sys.stderr)
        return 1
    result = _run(
        [sys.executable, "-m", "twine", "upload", *[str(a) for a in artifacts]],
    )
    if result != 0:
        print("❌ PyPI upload failed.")
        return result

    print("   ✅ PyPI upload done.")
    return 0


def _release_npm(dry_run: bool) -> int:
    # Remove stale wheels so npm pack only bundles the current version
    for stale_wheel in NPM_DIR.glob("voidx_cli-*.whl"):
        stale_wheel.unlink()
    # Copy bundled voidx_cli wheel from dist/ to npm/
    tui_wheel = list(DIST.glob("voidx_cli-*.whl"))
    if not tui_wheel:
        print("❌ No voidx_cli wheel found in dist/. Build PyPI first.")
        return 1
    wheel_src = tui_wheel[0]
    wheel_dst = NPM_DIR / wheel_src.name
    shutil.copy2(wheel_src, wheel_dst)
    print(f"   ✅ Bundled {wheel_src.name} → npm/")

    print("\n📦 Preparing npm package...")

    # Syntax check
    result = _run(["node", "--check", str(NPM_DIR / "bin" / "voidx.js")])
    if result != 0:
        print("❌ npm syntax check failed.")
        return result
    print("   ✅ Syntax check OK")

    if dry_run:
        print("   🏷️  Dry run — skipping npm publish.")
        return 0

    print("📤 Publishing to npm...")
    result = _run(
        ["npm", "publish", "--access", "public"],
        cwd=NPM_DIR,
    )
    if result != 0:
        print("❌ npm publish failed.")
        return result

    print("   ✅ npm publish done.")
    return 0


def _run(command: list[str], cwd: Path | None = None) -> int:
    try:
        subprocess.run(command, cwd=cwd or ROOT, check=True)
        return 0
    except subprocess.CalledProcessError as exc:
        return int(exc.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
