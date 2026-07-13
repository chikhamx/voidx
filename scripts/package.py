#!/usr/bin/env python3
"""Build voidx distribution artifacts on macOS, Linux, and Windows."""

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
BUILD = ROOT / "build"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build voidx package artifacts.")
    parser.add_argument("--format", choices=("wheel", "sdist", "all"), default="wheel")
    parser.add_argument("--out-dir", default=str(DIST), help="Output directory.")
    parser.add_argument("--clean", action="store_true", help="Remove output directory before building.")
    parser.add_argument("--check-only", action="store_true", help="Validate release metadata and exit.")
    parser.add_argument("--skip-checks", action="store_true", help="Skip release metadata checks.")
    parser.add_argument("--verify", action="store_true", help="Build wheels then verify they install correctly in a temp venv.")
    args = parser.parse_args()

    if not args.skip_checks:
        check_result = _check_release_metadata()
        if check_result != 0 or args.check_only:
            return check_result

    out_dir = Path(args.out_dir).expanduser().resolve()
    if args.clean and out_dir.exists():
        shutil.rmtree(out_dir)
    if args.clean and BUILD.exists():
        shutil.rmtree(BUILD)
    tui_build = ROOT / "tui" / "build"
    if args.clean and tui_build.exists():
        shutil.rmtree(tui_build)
    out_dir.mkdir(parents=True, exist_ok=True)

    package_roots = [ROOT]
    tui_root = ROOT / "tui"
    if (tui_root / "pyproject.toml").exists():
        package_roots.append(tui_root)

    for package_root in package_roots:
        result = _build_package(package_root, out_dir, args.format)
        if result != 0:
            return result

    if args.verify:
        result = _verify_wheels(out_dir)
        if result != 0:
            return result
    return 0


def _has_module(name: str) -> bool:
    probe = (
        "import importlib.util, sys\n"
        "try:\n"
        f"    spec = importlib.util.find_spec({name!r})\n"
        "except ModuleNotFoundError:\n"
        "    spec = None\n"
        "sys.exit(0 if spec else 1)"
    )
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT).returncode == 0


def _build_package(package_root: Path, out_dir: Path, fmt: str) -> int:
    if _has_module("build.__main__"):
        build_args = ["--wheel"] if fmt == "wheel" else ["--sdist"]
        if fmt == "all":
            build_args = ["--sdist", "--wheel"]
        return _run([sys.executable, "-m", "build", *build_args, "--outdir", str(out_dir), str(package_root)])

    uv = shutil.which("uv")
    if uv:
        uv_args = ["--wheel"] if fmt == "wheel" else ["--sdist"]
        if fmt == "all":
            uv_args = ["--sdist", "--wheel"]
        return _run([uv, "build", *uv_args, "--out-dir", str(out_dir), str(package_root)])

    if fmt == "wheel" and _has_module("pip") and _has_module("setuptools.build_meta"):
        return _run([
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(out_dir),
            str(package_root),
        ])

    print(
        "No packaging backend found. Install 'build', install uv, or ensure pip and setuptools are available for wheels.",
        file=sys.stderr,
    )
    return 1


def _check_release_metadata() -> int:
    errors: list[str] = []

    init_text = (ROOT / "src" / "voidx" / "__init__.py").read_text()
    init_match = re.search(r'__version__\s*=\s*"([^"]+)"', init_text)
    project_version = init_match.group(1) if init_match else ""
    if not project_version:
        errors.append("src/voidx/__init__.py is missing __version__.")


    tui_init = ROOT / "tui" / "voidx_cli" / "__init__.py"
    tui_pyproject = ROOT / "tui" / "pyproject.toml"
    if tui_init.exists() or tui_pyproject.exists():
        if not tui_init.exists():
            errors.append("tui/voidx_cli/__init__.py is missing.")
        else:
            tui_text = tui_init.read_text()
            tui_match = re.search(r'__version__\s*=\s*"([^"]+)"', tui_text)
            tui_version = tui_match.group(1) if tui_match else ""
            if tui_version != project_version:
                errors.append(
                    f"tui/voidx_cli/__init__.py version {tui_version or '<missing>'} "
                    f"does not match {project_version or '<missing>'}."
                )
        if not tui_pyproject.exists():
            errors.append("tui/pyproject.toml is missing.")
        else:
            tui_pyproject_text = tui_pyproject.read_text()
            if project_version and f'voidx=={project_version}' not in tui_pyproject_text:
                errors.append(f"tui/pyproject.toml must depend on voidx=={project_version}.")

    npm_package = ROOT / "npm" / "package.json"
    npm_bin = ROOT / "npm" / "bin" / "voidx.js"
    if npm_package.exists():
        npm_data = json.loads(npm_package.read_text())
        npm_version = npm_data.get("version", "")
        if npm_version != project_version:
            errors.append(
                f"npm/package.json version {npm_version or '<missing>'} "
                f"does not match __init__.py {project_version or '<missing>'}."
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


def _verify_wheels(out_dir: Path) -> int:
    """Install built wheels into a temp venv and verify imports succeed."""
    import tempfile

    wheels = sorted(out_dir.glob("*.whl"))
    if not wheels:
        print("No wheels found in output directory.", file=sys.stderr)
        return 1

    voidx_wheel = next((w for w in wheels if w.name.startswith("voidx-") and not w.name.startswith("voidx_cli-")), None)
    cli_wheel = next((w for w in wheels if w.name.startswith("voidx_cli-")), None)

    if not voidx_wheel:
        print("Missing voidx wheel.", file=sys.stderr)
        return 1

    print(f"\n🔍 Verifying wheel installation in temp venv…")
    print(f"   voidx:     {voidx_wheel.name}")
    print(f"   voidx_cli: {cli_wheel.name if cli_wheel else '(not built — skipping)'}")

    with tempfile.TemporaryDirectory(prefix="voidx-verify-") as tmp:
        venv_dir = Path(tmp) / "venv"
        result = _run([sys.executable, "-m", "venv", str(venv_dir)])
        if result != 0:
            print("Failed to create temp venv.", file=sys.stderr)
            return 1

        pip = str(venv_dir / "bin" / "pip")
        python = str(venv_dir / "bin" / "python")
        if sys.platform == "win32":
            pip = str(venv_dir / "Scripts" / "pip.exe")
            python = str(venv_dir / "Scripts" / "python.exe")

        # Upgrade pip
        _run([pip, "install", "--upgrade", "pip", "--quiet"])

        # Install voidx wheel
        result = _run([pip, "install", str(voidx_wheel), "--quiet"])
        if result != 0:
            print("pip install voidx failed.", file=sys.stderr)
            return 1

        # Install voidx_cli wheel (optional)
        if cli_wheel:
            result = _run([pip, "install", str(cli_wheel), "--quiet"])
            if result != 0:
                print("pip install voidx_cli failed.", file=sys.stderr)
                return 1

        # Verify imports
        probe = (
            "import voidx; print(f'voidx {voidx.__version__}')"
            + ("; import voidx_cli; print(f'voidx_cli {voidx_cli.__version__}')" if cli_wheel else "")
        )
        result = subprocess.run(
            [python, "-c", probe],
            capture_output=True, text=True,
            cwd=tmp,
        )
        if result.returncode != 0:
            print(f"Import verification failed:\n{result.stderr}", file=sys.stderr)
            return 1

        print(f"   ✅ {result.stdout.strip()}")

    print("   ✅ Wheel install verification passed.\n")
    return 0


def _run(command: list[str]) -> int:
    try:
        subprocess.run(command, cwd=ROOT, check=True)
        return 0
    except subprocess.CalledProcessError as exc:
        return int(exc.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
