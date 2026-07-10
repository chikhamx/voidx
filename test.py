#!/usr/bin/env python3
"""Run voidx test suites with concise, LLM-friendly output."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent

# Ensure we run under the voidx venv Python so sys.executable can find pytest.
_VOIDX_HOME = os.environ.get(
    "VOIDX_HOME", os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")) + "/voidx"
)
_VENV_PY = Path(_VOIDX_HOME) / "venv" / "bin" / "python"
if _VENV_PY.is_file() and str(_VENV_PY) != sys.executable:
    os.execv(str(_VENV_PY), [str(_VENV_PY), str(Path(__file__).resolve()), *sys.argv[1:]])

SUITES = ("backend", "frontend", "desktop")
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run voidx test suites.")
    parser.add_argument("--backend", action="store_true", help="Run backend pytest only.")
    parser.add_argument("--frontend", action="store_true", help="Run frontend vitest only.")
    parser.add_argument("--desktop", action="store_true", help="Run desktop cargo test only.")
    parser.add_argument("--verbose", "-v", action="store_true", help="Use native verbose runner output.")
    parser.add_argument(
        "--cargo-arg",
        action="append",
        default=[],
        metavar="ARG",
        help="Cargo option before '--' (repeat; use --cargo-arg=--features).",
    )
    args, extra = parser.parse_known_args()
    if extra and extra[0] == "--":
        extra = extra[1:]

    selected = [suite for suite in SUITES if getattr(args, suite)] or list(SUITES)
    results: list[tuple[str, str, int]] = []
    runner_error = False

    for suite in selected:
        if not _has_cmd_for(suite):
            print(f"⏭ {suite}: skipped ({_missing_reason(suite)})")
            results.append((suite, "SKIP", 0))
            continue

        try:
            if suite == "backend":
                status, code = _run_backend(extra, args.verbose)
            elif suite == "frontend":
                status, code = _run_frontend(extra, args.verbose)
            else:
                status, code = _run_desktop(extra, args.verbose, args.cargo_arg)
        except OSError as exc:
            runner_error = True
            status, code = "ERROR", 2
            print(f"❌ {suite}: runner error: {exc}")
        if status == "ERROR":
            runner_error = True
        results.append((suite, status, code))

    _summarize(results)
    return 2 if runner_error else 0


def _resolve_cmd(name: str) -> str | None:
    """Resolve a command via PATH plus known install locations."""
    found = shutil.which(name)
    if found:
        return found
    extra_dirs = {"cargo": [Path.home() / ".cargo" / "bin"]}
    for directory in extra_dirs.get(name, []):
        candidate = directory / name
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


def _build_backend_command(extra: list[str], *, verbose: bool) -> list[str]:
    command = [sys.executable, "-m", "pytest"]
    if not verbose:
        command.extend(
            [
                "-q",
                "--tb=short",
                "--disable-warnings",
                "--no-header",
                "-p",
                "scripts.pytest_compact",
            ]
        )
    if not extra:
        command.extend(["src/tests", "tui/tests"])
    command.extend(extra)
    return command


def _run_backend(extra: list[str], verbose: bool) -> tuple[str, int]:
    command = _build_backend_command(extra, verbose=verbose)
    code, output = _run_capture(command, cwd=ROOT)
    if verbose:
        _print_raw(output)
    else:
        _print_filtered(_filter_pytest_output(output))
    return _classify_pytest_status(code, output), code


def _classify_pytest_status(code: int, output: str) -> str:
    if code == 0:
        return "PASS"
    if code == 5:
        return "SKIP"
    clean = _strip_ansi(output)
    runner_error = re.search(
        r"^(?:INTERNALERROR|ERROR: usage:|pytest: error:)|"
        r"Error importing plugin|No module named ['\"]scripts\.pytest_compact",
        clean,
        re.MULTILINE,
    )
    if runner_error:
        return "ERROR"
    if re.search(
        r"^\d+ [A-Za-z]+(?:, \d+ [A-Za-z]+)*(?: in .+)?$",
        clean,
        re.MULTILINE,
    ):
        return "FAIL"
    if re.search(r"^ERROR: file or directory not found:", clean, re.MULTILINE):
        return "FAIL"
    if re.search(r"^no tests ran in ", clean, re.MULTILINE):
        return "FAIL"
    return "ERROR"


def _filter_pytest_output(text: str) -> str:
    """Keep pytest diagnostics and native category summary; remove decoration."""
    lines: list[str] = []
    skip_short_summary = False
    result_summary = re.compile(
        r"^\d+ [A-Za-z]+(?:, \d+ [A-Za-z]+)*(?: in .+)?$"
    )

    for raw_line in _strip_ansi(text).splitlines():
        stripped = raw_line.strip()
        if re.fullmatch(r"\[\s*\d+%\]", stripped):
            continue
        if "short test summary info" in stripped:
            skip_short_summary = True
            continue
        if skip_short_summary:
            if result_summary.match(stripped):
                lines.append(raw_line)
                skip_short_summary = False
            continue
        if stripped.startswith("-- Docs:"):
            continue
        if re.match(r"^=+ .+ =+$", stripped):
            continue
        if re.match(r"^_{5,}.+_{5,}$", stripped):
            continue
        lines.append(raw_line)

    return _collapse_blank_lines(lines)


def _build_frontend_command(extra: list[str], *, verbose: bool) -> list[str]:
    command = ["npm", "test"]
    if verbose:
        if extra:
            command.extend(["--", *extra])
        return command

    command.extend(["--silent", "--", "--reporter=agent", "--no-color", *extra])
    return command


def _run_frontend(extra: list[str], verbose: bool) -> tuple[str, int]:
    command = _build_frontend_command(extra, verbose=verbose)
    code, output = _run_capture(command, cwd=ROOT / "frontend")
    if verbose:
        _print_raw(output)
    else:
        _print_filtered(_filter_vitest_output(output))
    return _classify_vitest_status(code, output), code


def _classify_vitest_status(code: int, output: str) -> str:
    if code == 0:
        return "PASS"
    clean = _strip_ansi(output)
    runner_error = re.search(
        r"Transform failed|CACError:|Failed to load config|"
        r"Error: Build failed|Unhandled Error|Startup Error",
        clean,
        re.IGNORECASE,
    )
    if runner_error:
        return "ERROR"
    if re.search(r"Tests\s+\d+ failed", clean):
        return "FAIL"
    return "ERROR"


def _filter_vitest_output(text: str) -> str:
    """Keep Vitest diagnostics and summaries without npm/ANSI/time noise."""
    raw_lines = _strip_ansi(text).splitlines()
    lines: list[str] = []
    index = 0
    while index < len(raw_lines):
        raw_line = raw_lines[index]
        stripped = raw_line.strip()

        # Remove npm's two-line script header only when both lines match.
        if stripped == "> test" and index + 1 < len(raw_lines):
            next_stripped = raw_lines[index + 1].strip()
            if re.fullmatch(r"> vitest run(?:\s+.*)?", next_stripped):
                index += 2
                continue

        if re.fullmatch(r"\s+RUN\s+v\d+(?:\.\d+)+(?:\s+.*)?", raw_line):
            index += 1
            continue
        if stripped == "Not implemented: Window's alert() method":
            index += 1
            continue
        if stripped == "voidx: startup settings fallback failed socket not connected":
            index += 1
            continue
        if re.fullmatch(r"\s+Start at\s+\d{1,2}:\d{2}:\d{2}", raw_line):
            index += 1
            continue
        if re.fullmatch(
            r"\s+Duration\s+\d+(?:\.\d+)?(?:ms|s)(?:\s+\(.*\))?",
            raw_line,
        ):
            index += 1
            continue
        lines.append(raw_line)
        index += 1

    return _collapse_blank_lines(lines)


def _build_desktop_command(
    extra: list[str], *, verbose: bool, cargo_args: list[str]
) -> list[str]:
    cargo = _resolve_cmd("cargo") or "cargo"
    command = [cargo, "test"]
    if not verbose:
        command.append("-q")
    command.extend(cargo_args)

    test_args = list(extra)
    if verbose and "--nocapture" not in test_args:
        test_args.insert(0, "--nocapture")
    if test_args:
        command.extend(["--", *test_args])
    return command


def _run_desktop(
    extra: list[str], verbose: bool, cargo_args: list[str] | None = None
) -> tuple[str, int]:
    command = _build_desktop_command(extra, verbose=verbose, cargo_args=cargo_args or [])
    code, output = _run_capture(command, cwd=ROOT / "desktop" / "tauri")
    if verbose:
        _print_raw(output)
    else:
        _print_filtered(_filter_cargo_output(output))
    return _classify_cargo_status(code, output), code


def _classify_cargo_status(code: int, output: str) -> str:
    if code == 0:
        return "PASS"
    clean = _strip_ansi(output)
    runner_error = re.search(
        r"^(?:error\[[A-Z]\d+\]:|error: could not compile|"
        r"error: failed to (?:parse manifest|load source|run custom build command)|"
        r"error: (?:unexpected argument|no such command|no test target))",
        clean,
        re.MULTILINE,
    )
    if runner_error:
        return "ERROR"
    if re.search(r"test result: FAILED\. \d+ passed; [1-9]\d* failed", clean):
        return "FAIL"
    return "ERROR"


def _filter_cargo_output(text: str) -> str:
    """Preserve Cargo failures while aggregating per-binary result summaries."""
    lines: list[str] = []
    passed = 0
    failed = 0

    for raw_line in _strip_ansi(text).splitlines():
        stripped = raw_line.strip()
        if re.fullmatch(r"\.+", stripped):
            continue
        if re.match(r"^running \d+ tests?$", stripped):
            continue

        match = re.match(
            r"^test result: (ok|FAILED)\. (\d+) passed; (\d+) failed",
            stripped,
        )
        if match:
            passed += int(match.group(2))
            failed += int(match.group(3))
            continue
        lines.append(raw_line)

    if failed:
        lines.append(f"❌ {failed} failed, {passed} passed")
    elif passed:
        lines.append(f"✅ {passed} passed")
    return _collapse_blank_lines(lines)


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _collapse_blank_lines(lines: list[str]) -> str:
    compact: list[str] = []
    previous_blank = True
    for line in lines:
        is_blank = not line.strip()
        if is_blank and previous_blank:
            continue
        compact.append(line.rstrip())
        previous_blank = is_blank
    while compact and not compact[-1].strip():
        compact.pop()
    return "\n".join(compact)


def _print_filtered(text: str) -> None:
    if text:
        print(text)


def _print_raw(text: str) -> None:
    if text:
        print(text, end="" if text.endswith("\n") else "\n")


def _run_capture(
    command: list[str], cwd: Path, env: dict[str, str] | None = None
) -> tuple[int, str]:
    """Run a command with stderr merged into stdout to preserve ordering."""
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return int(result.returncode), result.stdout


def _run(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> int:
    result = subprocess.run(command, cwd=cwd, env=env)
    return int(result.returncode)


def _summarize(results: list[tuple[str, str, int]]) -> None:
    if len(results) == 1:
        name, status, _ = results[0]
        if status == "PASS":
            print(f"✅ {name} — passed")
        elif status == "SKIP":
            print(f"⏭ {name} — skipped")
        elif status == "ERROR":
            print(f"❌ {name} — runner error")
        return

    icons = {"PASS": "✅", "FAIL": "❌", "SKIP": "⏭", "ERROR": "❌"}
    summary = " | ".join(f"{icons[status]} {name}={status}" for name, status, _ in results)
    print(summary)


if __name__ == "__main__":
    raise SystemExit(main())