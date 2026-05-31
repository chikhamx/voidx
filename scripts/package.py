#!/usr/bin/env python3
"""Build voidx distribution artifacts on macOS, Linux, and Windows."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build voidx package artifacts.")
    parser.add_argument("--format", choices=("wheel", "sdist", "all"), default="wheel")
    parser.add_argument("--out-dir", default=str(DIST), help="Output directory.")
    parser.add_argument("--clean", action="store_true", help="Remove output directory before building.")
    args = parser.parse_args()

    out_dir = Path(args.out_dir).expanduser().resolve()
    if args.clean and out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if _has_module("build"):
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


def _run(command: list[str]) -> int:
    try:
        subprocess.run(command, cwd=ROOT, check=True)
        return 0
    except subprocess.CalledProcessError as exc:
        return int(exc.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
