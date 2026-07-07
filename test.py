#!/usr/bin/env python3
"""Run voidx test suites (backend pytest, frontend vitest, desktop cargo test)."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[0]

# Ensure we run under the voidx venv Python so sys.executable can find pytest.
_VOIDX_HOME = os.environ.get(
    "VOIDX_HOME", os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")) + "/voidx"
)
_VENV_PY = Path(_VOIDX_HOME) / "venv" / "bin" / "python"
if _VENV_PY.is_file() and str(_VENV_PY) != sys.executable:
    os.execv(str(_VENV_PY), [str(_VENV_PY), str(Path(__file__).resolve()), *sys.argv[1:]])

SUITES = ("backend", "frontend", "desktop")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run voidx test suites.")
    parser.add_argument("--backend", action="store_true", help="Run backend pytest only.")
    parser.add_argument("--frontend", action="store_true", help="Run frontend vitest only.")
    parser.add_argument("--desktop", action="store_true", help="Run desktop cargo test only.")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output.")
    parser.add_argument("--keep-going", action="store_true", help="Continue after failures.")
    args, extra = parser.parse_known_args()
    # argparse leaves the literal "--" in extra when used as a passthrough separator; strip it.
    if extra and extra[0] == "--":
        extra = extra[1:]

    selected = [s for s in SUITES if getattr(args, s)]
    if not selected:
        selected = list(SUITES)

    runners = {
        "backend": _run_backend,
        "frontend": _run_frontend,
        "desktop": _run_desktop,
    }

    results: list[tuple[str, str, int]] = []
    for suite in selected:
        if not _has_cmd_for(suite):
            print(f"⏭ {suite}: skipped ({_missing_reason(suite)})")
            results.append((suite, "SKIP", 0))
            continue
        status, code = runners[suite](extra, args.verbose)
        results.append((suite, status, code))
        if status == "FAIL" and not args.keep_going:
            break

    return _summarize(results)


def _resolve_cmd(name: str) -> str | None:
    """Resolve a command to its full path, checking PATH plus known install locations."""
    found = shutil.which(name)
    if found:
        return found
    extra_dirs = {
        "cargo": [Path.home() / ".cargo" / "bin"],
    }
    for d in extra_dirs.get(name, []):
        candidate = d / name
        if candidate.is_file():
            return str(candidate)
    return None


def _has_cmd(name: str) -> bool:
    return _resolve_cmd(name) is not None


def _has_cmd_for(suite: str) -> bool:
    if suite == "backend":
        return True
    if suite == "frontend":
        return _has_cmd("npm")
    if suite == "desktop":
        return _has_cmd("cargo")
    return False


def _missing_reason(suite: str) -> str:
    if suite == "frontend":
        return "npm not found"
    if suite == "desktop":
        return "cargo not found"
    return "unknown"


def _run_backend(extra: list[str], verbose: bool) -> tuple[str, int]:
    # 无透传参数时跑默认路径；有透传参数时让用户完全控制（支持 focused 测试）
    cmd = [sys.executable, "-m", "pytest"]
    if not extra:
        cmd.extend(["src/tests", "tui/tests"])
    if verbose:
        cmd.append("-v")
    cmd.extend(extra)
    code = _run(cmd, cwd=ROOT)
    return ("PASS" if code == 0 else "FAIL", code)


def _run_frontend(extra: list[str], verbose: bool) -> tuple[str, int]:
    # npm test [-- vitest_args...]
    # vitest 本身无 -v 等价物，verbose 仅影响 backend/desktop
    cmd = ["npm", "test"]
    passthrough = list(extra)
    if passthrough:
        cmd.append("--")
        cmd.extend(passthrough)
    code = _run(cmd, cwd=ROOT / "frontend")
    return ("PASS" if code == 0 else "FAIL", code)


def _run_desktop(extra: list[str], verbose: bool) -> tuple[str, int]:
    # cargo test [-- --nocapture] [-- test_args...]
    cargo = _resolve_cmd("cargo") or "cargo"
    cmd = [cargo, "test"]
    passthrough = list(extra)
    if verbose:
        passthrough.insert(0, "--nocapture")
    if passthrough:
        cmd.append("--")
        cmd.extend(passthrough)
    code = _run(cmd, cwd=ROOT / "desktop" / "tauri")
    return ("PASS" if code == 0 else "FAIL", code)


def _run(command: list[str], cwd: Path) -> int:
    try:
        result = subprocess.run(command, cwd=cwd)
        return int(result.returncode)
    except FileNotFoundError:
        return 1


def _summarize(results: list[tuple[str, str, int]]) -> int:
    print()
    print("─" * 40)
    for name, status, code in results:
        icon = {"PASS": "✅", "FAIL": "❌", "SKIP": "⏭"}[status]
        suffix = f" (exit {code})" if status == "FAIL" else ""
        print(f"  {icon} {name}: {status}{suffix}")
    print("─" * 40)

    failed = [r for r in results if r[1] == "FAIL"]
    if failed:
        print(f"\n{len(failed)} suite(s) failed.")
        return 1
    print("\nAll suites passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
