#!/usr/bin/env python3
"""Run voidx test suites with concise, LLM-friendly output."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def _workspace_pythonpath() -> None:
    paths = [str(ROOT / "src"), str(ROOT)]
    existing = os.environ.get("PYTHONPATH")
    if existing:
        paths.extend(
            entry
            for entry in existing.split(os.pathsep)
            if entry and entry not in paths
        )
    os.environ["PYTHONPATH"] = os.pathsep.join(paths)


_workspace_pythonpath()

# Ensure we run under the voidx venv Python so sys.executable can find pytest.
_VOIDX_HOME = os.environ.get(
    "VOIDX_HOME", os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")) + "/voidx"
)
_VENV_PY = Path(_VOIDX_HOME) / "venv" / "bin" / "python"
if _VENV_PY.is_file() and str(_VENV_PY) != sys.executable:
    os.execv(str(_VENV_PY), [str(_VENV_PY), str(Path(__file__).resolve()), *sys.argv[1:]])



SUITES = ("backend", "frontend", "desktop")
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class SuiteStatus(str, Enum):
    """Suite execution terminal state."""

    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"
    ERROR = "ERROR"


@dataclass
class TestFailure:
    """Single test failure record."""

    test_id: str
    file_path: str
    test_name: str
    message: str


@dataclass
class SkipRecord:
    """Single skip record."""

    reason: str


@dataclass
class SuiteResult:
    """Complete result for a single suite."""

    name: str
    status: SuiteStatus
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: list[TestFailure] = field(default_factory=list)
    skipped_details: list[SkipRecord] = field(default_factory=list)
    command: list[str] = field(default_factory=list)
    output: str = ""
    exit_code: int = 0
    duration_seconds: float = 0.0


_RUNNER_ERROR_STATES = {SuiteStatus.ERROR}


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
    results: list[SuiteResult] = []

    for suite in selected:
        if not _has_cmd_for(suite):
            reason = _missing_reason(suite)
            results.append(
                SuiteResult(
                    name=suite,
                    status=SuiteStatus.SKIP,
                    skipped=1,
                    skipped_details=[SkipRecord(reason=reason)],
                )
            )
            continue

        try:
            if suite == "backend":
                result = _run_backend(extra, args.verbose)
            elif suite == "frontend":
                result = _run_frontend(extra, args.verbose)
            else:
                result = _run_desktop(extra, args.verbose, args.cargo_arg)
        except OSError as exc:
            result = SuiteResult(
                name=suite,
                status=SuiteStatus.ERROR,
                errors=[
                    TestFailure(
                        test_id=suite,
                        file_path="",
                        test_name=suite,
                        message=str(exc),
                    )
                ],
                exit_code=2,
                output=str(exc),
            )
        results.append(result)

    runner_error = any(result.status in _RUNNER_ERROR_STATES for result in results)
    exit_code = 2 if runner_error else 0
    _print_json_results(results, exit_code)
    return exit_code


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


def _run_backend(extra: list[str], verbose: bool) -> SuiteResult:
    command = _build_backend_command(extra, verbose=verbose)
    started = time.monotonic()
    code, output = _run_capture(command, cwd=ROOT)
    duration = time.monotonic() - started
    return _build_suite_result(
        "backend",
        _classify_pytest_status(code, output),
        command,
        code,
        duration,
        output,
        output if verbose else _filter_pytest_output(output),
    )


def _classify_pytest_status(code: int, output: str) -> SuiteStatus:
    if code == 0:
        return SuiteStatus.PASS
    if code == 5:
        return SuiteStatus.SKIP
    clean = _strip_ansi(output)
    # Only test.py's own failures (plugin/config bugs) should be ERROR.
    # User code errors (conftest.py, test file imports, syntax) are FAIL.
    runner_error = re.search(
        r"^(?:ERROR: usage:|pytest: error:)|"
        r"Error importing plugin|No module named ['\"]scripts\.pytest_compact",
        clean,
        re.MULTILINE,
    )
    if runner_error:
        return SuiteStatus.ERROR
    if re.search(
        r"^\d+ [A-Za-z]+(?:, \d+ [A-Za-z]+)*(?: in .+)?$",
        clean,
        re.MULTILINE,
    ):
        return SuiteStatus.FAIL
    if re.search(r"^ERROR: file or directory not found:", clean, re.MULTILINE):
        return SuiteStatus.FAIL
    if re.search(r"^no tests ran in ", clean, re.MULTILINE):
        return SuiteStatus.FAIL
    if re.search(
        r"^(?:INTERNALERROR|ImportError while importing test module)",
        clean,
        re.MULTILINE,
    ):
        return SuiteStatus.FAIL
    return SuiteStatus.FAIL


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


def _run_frontend(extra: list[str], verbose: bool) -> SuiteResult:
    command = _build_frontend_command(extra, verbose=verbose)
    started = time.monotonic()
    code, output = _run_capture(command, cwd=ROOT / "frontend")
    duration = time.monotonic() - started
    return _build_suite_result(
        "frontend",
        _classify_vitest_status(code, output),
        command,
        code,
        duration,
        output,
        output if verbose else _filter_vitest_output(output),
    )


def _classify_vitest_status(code: int, output: str) -> SuiteStatus:
    if code == 0:
        return SuiteStatus.PASS
    clean = _strip_ansi(output)
    # User code/build errors are FAIL; only test.py's own bugs are ERROR.
    if re.search(r"Tests\s+\d+ failed", clean):
        return SuiteStatus.FAIL
    return SuiteStatus.FAIL


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
) -> SuiteResult:
    command = _build_desktop_command(extra, verbose=verbose, cargo_args=cargo_args or [])
    started = time.monotonic()
    code, output = _run_capture(command, cwd=ROOT / "desktop" / "tauri")
    duration = time.monotonic() - started
    return _build_suite_result(
        "desktop",
        _classify_cargo_status(code, output),
        command,
        code,
        duration,
        output,
        output if verbose else _filter_cargo_output(output),
    )


def _classify_cargo_status(code: int, output: str) -> SuiteStatus:
    if code == 0:
        return SuiteStatus.PASS
    clean = _strip_ansi(output)
    # Only test.py's own failures (bad args/commands) should be ERROR.
    # User code errors (compile, manifest, build) are FAIL.
    runner_error = re.search(
        r"^error: (?:unexpected argument|no such command|no test target)",
        clean,
        re.MULTILINE,
    )
    if runner_error:
        return SuiteStatus.ERROR
    if re.search(r"test result: FAILED\. \d+ passed; [1-9]\d* failed", clean):
        return SuiteStatus.FAIL
    return SuiteStatus.FAIL


def _build_suite_result(
    name: str,
    status: SuiteStatus,
    command: list[str],
    exit_code: int,
    duration_seconds: float,
    raw_output: str,
    output: str | None = None,
) -> SuiteResult:
    output = raw_output if output is None else output
    passed, failed, skipped = _extract_counts(name, raw_output)
    errors = _extract_failures(name, raw_output) if status in {SuiteStatus.FAIL, SuiteStatus.ERROR} else []
    return SuiteResult(
        name=name,
        status=status,
        passed=passed,
        failed=failed,
        skipped=skipped,
        errors=errors,
        command=command,
        exit_code=exit_code,
        duration_seconds=duration_seconds,
        output=output,
    )


def _extract_counts(suite: str, output: str) -> tuple[int, int, int]:
    clean = _strip_ansi(output)
    if suite == "frontend":
        return _extract_vitest_counts(clean)
    if suite == "desktop":
        return _extract_cargo_counts(clean)
    return _extract_pytest_counts(clean)


def _extract_pytest_counts(output: str) -> tuple[int, int, int]:
    passed = failed = skipped = 0
    summary_match = None
    for line in output.splitlines():
        if re.search(r"\b(?:passed|failed|error|errors|skipped|xfailed|xpassed)\b", line):
            summary_match = line.strip()
    if not summary_match:
        return passed, failed, skipped
    for number, label in re.findall(r"(\d+)\s+([A-Za-z]+)", summary_match):
        value = int(number)
        label = label.lower()
        if label == "passed":
            passed += value
        elif label in {"failed", "error", "errors"}:
            failed += value
        elif label == "skipped":
            skipped += value
    return passed, failed, skipped


def _extract_vitest_counts(output: str) -> tuple[int, int, int]:
    passed = failed = skipped = 0
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped.startswith("Tests"):
            continue
        for number, label in re.findall(r"(\d+)\s+(failed|passed|skipped)", stripped):
            value = int(number)
            if label == "passed":
                passed += value
            elif label == "failed":
                failed += value
            elif label == "skipped":
                skipped += value
    return passed, failed, skipped


def _extract_cargo_counts(output: str) -> tuple[int, int, int]:
    passed = failed = skipped = 0
    pattern = re.compile(r"test result: (?:ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored")
    for match in pattern.finditer(output):
        passed += int(match.group(1))
        failed += int(match.group(2))
        skipped += int(match.group(3))
    return passed, failed, skipped


def _extract_failures(suite: str, output: str) -> list[TestFailure]:
    clean = _strip_ansi(output)
    if suite == "frontend":
        return _extract_vitest_failures(clean)
    if suite == "desktop":
        return _extract_cargo_failures(clean)
    return _extract_pytest_failures(clean)


def _extract_pytest_failures(output: str) -> list[TestFailure]:
    failures: list[TestFailure] = []
    for line in output.splitlines():
        match = re.match(r"FAILED\s+([^\s]+)::([^\s]+)\s+-\s+(.+)", line.strip())
        if not match:
            continue
        file_path = match.group(1)
        test_name = match.group(2)
        failures.append(
            TestFailure(
                test_id=f"{file_path}::{test_name}",
                file_path=file_path,
                test_name=test_name,
                message=match.group(3),
            )
        )
    return failures


def _extract_vitest_failures(output: str) -> list[TestFailure]:
    failures: list[TestFailure] = []
    lines = output.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"\s*FAIL\s+(.+)", line)
        if not match:
            continue
        test_id = match.group(1).strip()
        parts = [part.strip() for part in test_id.split(" > ") if part.strip()]
        file_path = parts[0] if parts else test_id
        test_name = parts[-1] if parts else test_id
        message = ""
        for next_line in lines[index + 1 :]:
            stripped = next_line.strip()
            if stripped:
                message = stripped
                break
        failures.append(
            TestFailure(
                test_id=test_id,
                file_path=file_path,
                test_name=test_name,
                message=message,
            )
        )
    return failures


def _extract_cargo_failures(output: str) -> list[TestFailure]:
    failures: list[TestFailure] = []
    for line in output.splitlines():
        match = re.match(r"test\s+(.+?)\s+\.\.\.\s+FAILED", line.strip())
        if not match:
            continue
        test_id = match.group(1)
        failures.append(
            TestFailure(
                test_id=test_id,
                file_path="",
                test_name=test_id.split("::")[-1],
                message="test failed",
            )
        )
    return failures


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


def to_dict(result: SuiteResult) -> dict:
    payload = {
        "status": result.status.value,
        "passed": result.passed,
        "failed": result.failed,
        "skipped": result.skipped,
        "exit_code": result.exit_code,
        "duration": _format_duration(result.duration_seconds),
        "output": result.output,
    }
    if result.errors:
        payload["errors"] = [
            {
                "test_id": error.test_id,
                "file_path": error.file_path,
                "test_name": error.test_name,
                "message": error.message,
            }
            for error in result.errors
        ]
    if result.skipped_details:
        payload["skipped_details"] = [
            {"reason": skip.reason} for skip in result.skipped_details
        ]
    return payload


def _format_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    value = f"{seconds:.2f}".rstrip("0").rstrip(".")
    return f"{value} s"


def _print_json_results(results: list[SuiteResult], exit_code: int) -> None:
    print(json.dumps({"results": [to_dict(result) for result in results], "exit_code": exit_code}))


def _summarize(results: list[SuiteResult]) -> None:
    if len(results) == 1:
        result = results[0]
        if result.status is SuiteStatus.PASS:
            print(f"✅ {result.name} — passed")
        elif result.status is SuiteStatus.SKIP:
            print(f"⏭ {result.name} — skipped")
        elif result.status is SuiteStatus.ERROR:
            print(f"❌ {result.name} — runner error")
        return

    icons = {
        SuiteStatus.PASS: "✅",
        SuiteStatus.FAIL: "❌",
        SuiteStatus.SKIP: "⏭",
        SuiteStatus.ERROR: "❌",
    }
    summary = " | ".join(
        f"{icons[result.status]} {result.name}={result.status.value}" for result in results
    )
    print(summary)


if __name__ == "__main__":
    raise SystemExit(main())