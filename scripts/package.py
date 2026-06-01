#!/usr/bin/env python3
"""Build voidx distribution artifacts on macOS, Linux, and Windows."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build voidx package artifacts.")
    parser.add_argument("--format", choices=("wheel", "sdist", "all"), default="wheel")
    parser.add_argument("--out-dir", default=str(DIST), help="Output directory.")
    parser.add_argument("--clean", action="store_true", help="Remove output directory before building.")
    parser.add_argument("--check-only", action="store_true", help="Validate release metadata and exit.")
    parser.add_argument("--skip-checks", action="store_true", help="Skip release metadata checks.")
    args = parser.parse_args()

    if not args.skip_checks:
        check_result = _check_release_metadata()
        if check_result != 0 or args.check_only:
            return check_result

    out_dir = Path(args.out_dir).expanduser().resolve()
    if args.clean and out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if _has_module("build.__main__"):
        build_args = ["--wheel"] if args.format == "wheel" else ["--sdist"]
        if args.format == "all":
            build_args = ["--sdist", "--wheel"]
        return _run([sys.executable, "-m", "build", *build_args, "--outdir", str(out_dir), str(ROOT)])

    if args.format == "wheel" and _has_module("pip"):
        return _run([
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(out_dir),
            str(ROOT),
        ])

    uv = shutil.which("uv")
    if uv:
        uv_args = ["--wheel"] if args.format == "wheel" else ["--sdist"]
        if args.format == "all":
            uv_args = ["--sdist", "--wheel"]
        return _run([uv, "build", *uv_args, "--out-dir", str(out_dir), str(ROOT)])

    print(
        "No packaging backend found. Install 'build', ensure pip is available for wheels, "
        "or install uv.",
        file=sys.stderr,
    )
    return 1


def _has_module(name: str) -> bool:
    probe = (
        "import importlib.util, sys; "
        f"sys.exit(0 if importlib.util.find_spec({name!r}) else 1)"
    )
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT).returncode == 0


def _check_release_metadata() -> int:
    errors: list[str] = []
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project_version = pyproject["project"]["version"]

    init_text = (ROOT / "src" / "voidx" / "__init__.py").read_text()
    init_match = re.search(r'__version__\s*=\s*"([^"]+)"', init_text)
    init_version = init_match.group(1) if init_match else ""
    if init_version != project_version:
        errors.append(
            f"src/voidx/__init__.py version {init_version or '<missing>'} "
            f"does not match pyproject.toml {project_version}."
        )

    npm_package = ROOT / "npm" / "package.json"
    npm_bin = ROOT / "npm" / "bin" / "voidx.js"
    if npm_package.exists():
        npm_data = json.loads(npm_package.read_text())
        npm_version = npm_data.get("version", "")
        if npm_version != project_version:
            errors.append(
                f"npm/package.json version {npm_version or '<missing>'} "
                f"does not match pyproject.toml {project_version}."
            )
        if npm_data.get("bin", {}).get("voidx") != "bin/voidx.js":
            errors.append("npm/package.json must expose bin.voidx as bin/voidx.js.")
        if not npm_bin.exists():
            errors.append("npm/bin/voidx.js is missing.")
    else:
        errors.append("npm/package.json is missing.")

    if errors:
        for error in errors:
            print(f"release metadata error: {error}", file=sys.stderr)
        return 1
    return 0


def _run(command: list[str]) -> int:
    try:
        subprocess.run(command, cwd=ROOT, check=True)
        return 0
    except subprocess.CalledProcessError as exc:
        return int(exc.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
