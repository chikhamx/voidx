from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
RUNNER_PATH = ROOT / "test.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("voidx_test_runner", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run_runner(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(RUNNER_PATH), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _temporary_test(source: str) -> Path:
    directory = Path(tempfile.mkdtemp(prefix=".test-runner-", dir=ROOT))
    path = directory / "test_sample.py"
    path.write_text(source, encoding="utf-8")
    return path


def test_suite_status_is_string_compatible_enum() -> None:
    runner = _load_runner()

    assert runner.SuiteStatus.PASS == "PASS"
    assert runner.SuiteStatus.FAIL == "FAIL"
    assert runner._classify_pytest_status(0, "") is runner.SuiteStatus.PASS
    assert runner._classify_pytest_status(5, "") is runner.SuiteStatus.SKIP
    assert runner._classify_vitest_status(1, "Tests  1 failed (1)") is runner.SuiteStatus.FAIL
    assert runner._classify_cargo_status(101, "error[E0308]: mismatched types") is runner.SuiteStatus.FAIL


def test_suite_result_serializes_to_plain_json_ready_dict() -> None:
    runner = _load_runner()
    result = runner.SuiteResult(
        name="backend",
        status=runner.SuiteStatus.FAIL,
        passed=2,
        failed=1,
        skipped=1,
        errors=[
            runner.TestFailure(
                test_id="src/tests/test_sample.py::test_broken",
                file_path="src/tests/test_sample.py",
                test_name="test_broken",
                message="AssertionError: expected truthy value",
            )
        ],
        skipped_details=[runner.SkipRecord(reason="npm not found")],
        command=["python", "-m", "pytest"],
        exit_code=1,
        duration_seconds=0.25,
    )

    assert runner.to_dict(result) == {
        "status": "FAIL",
        "passed": 2,
        "failed": 1,
        "skipped": 1,
        "exit_code": 1,
        "duration": "250 ms",
        "output": "",
        "errors": [
            {
                "test_id": "src/tests/test_sample.py::test_broken",
                "file_path": "src/tests/test_sample.py",
                "test_name": "test_broken",
                "message": "AssertionError: expected truthy value",
            }
        ],
        "skipped_details": [{"reason": "npm not found"}],
    }

    passed_result = runner.to_dict(
        runner.SuiteResult(
            name="backend",
            status=runner.SuiteStatus.PASS,
            duration_seconds=1.25,
        )
    )
    assert passed_result == {
        "status": "PASS",
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "exit_code": 0,
        "duration": "1.25 s",
        "output": "",
    }
    assert "name" not in passed_result
    assert "errors" not in passed_result
    assert "skipped_details" not in passed_result


def test_summarize_consumes_suite_results_without_output_change(capsys) -> None:
    runner = _load_runner()

    runner._summarize([runner.SuiteResult(name="backend", status=runner.SuiteStatus.PASS)])
    runner._summarize([
        runner.SuiteResult(name="backend", status=runner.SuiteStatus.PASS),
        runner.SuiteResult(name="frontend", status=runner.SuiteStatus.FAIL),
    ])

    assert capsys.readouterr().out.splitlines() == [
        "✅ backend — passed",
        "✅ backend=PASS | ❌ frontend=FAIL",
    ]


def test_build_suite_result_extracts_counts_and_failure_details() -> None:
    runner = _load_runner()
    pytest_output = "\n".join(
        [
            "FAILED src/tests/test_sample.py::test_broken - AssertionError: expected truthy value",
            "2 failed, 3 passed, 1 skipped in 0.10s",
        ]
    )
    vitest_output = "\n".join(
        [
            " FAIL  frontend/src/sample.test.ts > sample > renders",
            "AssertionError: expected true to be false",
            " Test Files  1 failed (1)",
            "      Tests  2 failed | 4 passed | 1 skipped (7)",
        ]
    )
    cargo_output = "\n".join(
        [
            "test tests::it_fails ... FAILED",
            "test result: FAILED. 5 passed; 1 failed; 2 ignored; 0 measured",
        ]
    )

    pytest_result = runner._build_suite_result(
        "backend",
        runner.SuiteStatus.FAIL,
        ["python", "-m", "pytest"],
        1,
        0.1,
        pytest_output,
    )
    vitest_result = runner._build_suite_result(
        "frontend",
        runner.SuiteStatus.FAIL,
        ["npm", "test"],
        1,
        0.2,
        vitest_output,
    )
    cargo_result = runner._build_suite_result(
        "desktop",
        runner.SuiteStatus.FAIL,
        ["cargo", "test"],
        101,
        0.3,
        cargo_output,
    )

    assert (pytest_result.passed, pytest_result.failed, pytest_result.skipped) == (3, 2, 1)
    assert pytest_result.errors == [
        runner.TestFailure(
            test_id="src/tests/test_sample.py::test_broken",
            file_path="src/tests/test_sample.py",
            test_name="test_broken",
            message="AssertionError: expected truthy value",
        )
    ]
    assert (vitest_result.passed, vitest_result.failed, vitest_result.skipped) == (4, 2, 1)
    assert vitest_result.errors[0].test_id == "frontend/src/sample.test.ts > sample > renders"
    assert vitest_result.errors[0].file_path == "frontend/src/sample.test.ts"
    assert vitest_result.errors[0].test_name == "renders"
    assert "AssertionError" in vitest_result.errors[0].message
    assert (cargo_result.passed, cargo_result.failed, cargo_result.skipped) == (5, 1, 2)
    assert cargo_result.errors[0].test_id == "tests::it_fails"
    assert cargo_result.errors[0].test_name == "it_fails"



def test_main_prints_structured_json_to_stdout(monkeypatch, capsys) -> None:
    runner = _load_runner()

    def fake_backend(extra: list[str], verbose: bool):
        return runner.SuiteResult(
            name="backend",
            status=runner.SuiteStatus.FAIL,
            passed=1,
            failed=1,
            command=["python", "-m", "pytest"],
            exit_code=1,
            duration_seconds=0.1,
            errors=[
                runner.TestFailure(
                    test_id="src/tests/test_sample.py::test_broken",
                    file_path="src/tests/test_sample.py",
                    test_name="test_broken",
                    message="AssertionError",
                )
            ],
        )

    monkeypatch.setattr(runner, "_run_backend", fake_backend)
    monkeypatch.setattr(sys, "argv", ["test.py", "--backend"])

    assert runner.main() == 0
    payload = __import__("json").loads(capsys.readouterr().out)
    assert payload == {
        "results": [
            {
                "status": "FAIL",
                "passed": 1,
                "failed": 1,
                "skipped": 0,
                "exit_code": 1,
                "duration": "100 ms",
                "output": "",
                "errors": [
                    {
                        "test_id": "src/tests/test_sample.py::test_broken",
                        "file_path": "src/tests/test_sample.py",
                        "test_name": "test_broken",
                        "message": "AssertionError",
                    }
                ],
            }
        ],
        "exit_code": 0,
    }

def test_main_returns_nonzero_for_runner_infrastructure_error(monkeypatch, capsys) -> None:
    runner = _load_runner()

    def fail_runner(extra: list[str], verbose: bool):
        raise FileNotFoundError("missing runner cwd")

    monkeypatch.setattr(runner, "_run_backend", fail_runner)
    monkeypatch.setattr(sys, "argv", ["test.py", "--backend"])

    assert runner.main() == 2
    payload = __import__("json").loads(capsys.readouterr().out)
    assert payload["exit_code"] == 2
    assert payload["results"][0]["status"] == "ERROR"
    assert payload["results"][0]["output"] == "missing runner cwd"
    assert payload["results"][0]["errors"][0]["message"] == "missing runner cwd"


def test_vitest_filter_preserves_real_stderr_and_strips_only_noise() -> None:
    runner = _load_runner()
    raw = "\n".join(
        [
            "\x1b[36m RUN  v4.1.9 /repo/frontend\x1b[0m",
            "Not implemented: Window's alert() method",
            "stderr | test/example.test.ts > example",
            "> REAL ERROR CONTEXT",
            "actual failure detail",
            " Test Files  1 failed (1)",
            "      Tests  1 failed (1)",
            "   Start at  10:00:00",
            "   Duration  1.00s",
        ]
    )

    filtered = runner._filter_vitest_output(raw)

    assert "\x1b" not in filtered
    assert "RUN  v" not in filtered
    assert "Window's alert" not in filtered
    assert "stderr | test/example.test.ts" in filtered
    assert "> REAL ERROR CONTEXT" in filtered
    assert "actual failure detail" in filtered
    assert "Start at" not in filtered
    assert "Duration" not in filtered


def test_cargo_filter_aggregates_binary_summaries() -> None:
    runner = _load_runner()
    raw = "\n".join(
        [
            "running 3 tests",
            "...",
            "test result: ok. 3 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out",
            "running 5 tests",
            ".....",
            "test result: ok. 5 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out",
        ]
    )

    assert runner._filter_cargo_output(raw).strip() == "✅ 8 passed"


def test_verbose_commands_preserve_native_runner_output() -> None:
    runner = _load_runner()

    assert runner._build_backend_command([], verbose=True) == [
        sys.executable,
        "-m",
        "pytest",
        "src/tests",
        "tui/tests",
    ]
    assert runner._build_frontend_command([], verbose=True) == ["npm", "test"]
    assert runner._build_desktop_command([], verbose=True, cargo_args=[]) == [
        runner._resolve_cmd("cargo") or "cargo",
        "test",
        "--",
        "--nocapture",
    ]


def test_cargo_command_separates_cargo_and_test_arguments() -> None:
    runner = _load_runner()

    command = runner._build_desktop_command(
        ["--nocapture"],
        verbose=False,
        cargo_args=["--features", "experimental"],
    )

    assert command == [
        runner._resolve_cmd("cargo") or "cargo",
        "test",
        "-q",
        "--features",
        "experimental",
        "--",
        "--nocapture",
    ]


def test_tui_only_run_uses_compact_pytest_output() -> None:
    result = _run_runner("--backend", "--", "tui/tests/test_headless.py")

    assert result.returncode == 0
    payload = __import__("json").loads(result.stdout)
    output = payload["results"][0]["output"]
    assert "[100%]" not in result.stdout
    assert "test_headless.py" not in output
    assert "passed" in output


@pytest.mark.parametrize(
    "source, expected",
    [
        (
            "import pytest\n\n@pytest.fixture\ndef broken():\n    raise RuntimeError('setup broke')\n\ndef test_value(broken):\n    pass\n",
            "1 error",
        ),
        ("def test_broken(:\n    pass\n", "1 error"),
    ],
)
def test_pytest_phase_errors_are_not_reported_as_success(source: str, expected: str) -> None:
    test_path = _temporary_test(source)
    try:
        result = _run_runner("--backend", "--", str(test_path.relative_to(ROOT)))
    finally:
        shutil.rmtree(test_path.parent, ignore_errors=True)

    assert result.returncode == 0
    payload = __import__("json").loads(result.stdout)
    assert expected in payload["results"][0]["output"]
    assert payload["results"][0]["status"] == "FAIL"


def test_pytest_summary_preserves_skip_and_xfail_categories() -> None:
    test_path = _temporary_test(
        "import pytest\n\ndef test_pass():\n    pass\n\ndef test_skip():\n    pytest.skip('later')\n\n@pytest.mark.xfail(reason='known')\ndef test_xfail():\n    assert False\n"
    )
    try:
        result = _run_runner("--backend", "--", str(test_path.relative_to(ROOT)))
    finally:
        shutil.rmtree(test_path.parent, ignore_errors=True)

    assert result.returncode == 0
    payload = __import__("json").loads(result.stdout)
    output = payload["results"][0]["output"]
    assert "1 passed" in output
    assert "1 skipped" in output
    assert "1 xfailed" in output


def test_pytest_usage_error_returns_nonzero() -> None:
    result = _run_runner("--backend", "--", "--definitely-invalid-option")

    assert result.returncode == 2
    payload = __import__("json").loads(result.stdout)
    assert payload["exit_code"] == 2
    assert "unrecognized arguments" in payload["results"][0]["output"]


def test_pytest_filter_preserves_warning_and_deselected_summary() -> None:
    runner = _load_runner()
    raw = "\n".join(
        [
            "================ short test summary info ================",
            "FAILED test_sample.py::test_failure",
            "1 failed, 2 deselected, 1 warning in 0.10s",
        ]
    )

    filtered = runner._filter_pytest_output(raw)

    assert "FAILED test_sample.py" not in filtered
    assert "1 failed, 2 deselected, 1 warning in 0.10s" in filtered


def test_vitest_filter_preserves_diagnostic_lines_with_metadata_prefixes() -> None:
    runner = _load_runner()
    raw = "\n".join(
        [
            "   Start at  10:00:00",
            "   Duration  1.00s",
            "Start at invalid state",
            "Duration exceeded threshold",
        ]
    )

    filtered = runner._filter_vitest_output(raw)

    assert "Start at  10:00:00" not in filtered
    assert "Duration  1.00s" not in filtered
    assert "Start at invalid state" in filtered
    assert "Duration exceeded threshold" in filtered


def test_pytest_compact_classifies_phase_xfail_and_xpass() -> None:
    from types import SimpleNamespace

    from scripts.pytest_compact import pytest_report_teststatus

    setup_xfail = SimpleNamespace(
        when="setup", wasxfail="known", skipped=True, passed=False, failed=False
    )
    teardown_xfail = SimpleNamespace(
        when="teardown", wasxfail="known", skipped=True, passed=False, failed=False
    )
    call_xpass = SimpleNamespace(
        when="call", wasxfail="known", skipped=False, passed=True, failed=False
    )

    assert pytest_report_teststatus(setup_xfail, None)[0] == "xfailed"
    assert pytest_report_teststatus(teardown_xfail, None)[0] == "xfailed"
    assert pytest_report_teststatus(call_xpass, None)[0] == "xpassed"


def test_cargo_cli_parses_cargo_args_separately(monkeypatch) -> None:
    runner = _load_runner()
    captured: dict[str, object] = {}

    def fake_desktop(extra: list[str], verbose: bool, cargo_args: list[str]):
        captured["extra"] = extra
        captured["verbose"] = verbose
        captured["cargo_args"] = cargo_args
        return runner.SuiteResult(
            name="desktop",
            status=runner.SuiteStatus.PASS,
            command=["cargo", "test"],
            exit_code=0,
        )

    monkeypatch.setattr(runner, "_has_cmd_for", lambda suite: True)
    monkeypatch.setattr(runner, "_run_desktop", fake_desktop)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "test.py",
            "--desktop",
            "--cargo-arg=--features",
            "--cargo-arg=experimental",
            "--",
            "--nocapture",
        ],
    )

    assert runner.main() == 0
    assert captured == {
        "extra": ["--nocapture"],
        "verbose": False,
        "cargo_args": ["--features", "experimental"],
    }


def test_runner_status_classification_distinguishes_failures_from_errors() -> None:
    runner = _load_runner()

    assert runner._classify_pytest_status(2, "1 error in 0.10s") == "FAIL"
    assert runner._classify_pytest_status(2, "pytest: error: unrecognized arguments") == "ERROR"
    assert runner._classify_vitest_status(
        1, "Test Files  1 failed (1)\nTests  1 failed (1)"
    ) == "FAIL"
    assert runner._classify_vitest_status(1, "CACError: Unknown option") == "FAIL"
    assert runner._classify_cargo_status(
        101, "test result: FAILED. 0 passed; 1 failed"
    ) == "FAIL"
    assert runner._classify_cargo_status(101, "error[E0308]: mismatched types") == "FAIL"


def test_pytest_status_treats_plugin_startup_failure_as_runner_error() -> None:
    runner = _load_runner()

    output = "ImportError: Error importing plugin 'missing_plugin': No module named missing_plugin"
    assert runner._classify_pytest_status(1, output) == "ERROR"


def test_vitest_status_treats_transform_failure_as_user_error() -> None:
    runner = _load_runner()

    output = "\n".join(
        [
            "Error: Transform failed with 1 error:",
            "Test Files  1 failed (1)",
            "Tests  no tests",
        ]
    )
    assert runner._classify_vitest_status(1, output) == "FAIL"


def test_pytest_compact_handles_empty_xfail_reason() -> None:
    from types import SimpleNamespace

    from scripts.pytest_compact import pytest_report_teststatus

    xfail = SimpleNamespace(
        when="call", wasxfail="", skipped=True, passed=False, failed=False
    )
    xpass = SimpleNamespace(
        when="call", wasxfail="", skipped=False, passed=True, failed=False
    )

    assert pytest_report_teststatus(xfail, None)[0] == "xfailed"
    assert pytest_report_teststatus(xpass, None)[0] == "xpassed"


def test_vitest_filter_only_removes_exact_npm_command_metadata() -> None:
    runner = _load_runner()
    raw = "\n".join(
        [
            "> test",
            "> vitest run --reporter=agent --no-color",
            "> vitest runner crashed while parsing fixture",
        ]
    )

    filtered = runner._filter_vitest_output(raw)

    assert "> test" not in filtered
    assert "> vitest run --reporter=agent --no-color" not in filtered
    assert "> vitest runner crashed while parsing fixture" in filtered


def test_runner_error_markers_override_failure_summaries() -> None:
    runner = _load_runner()

    pytest_output = "INTERNALERROR> unexpected pytest crash\n1 failed in 0.10s"
    vitest_output = "Error: Transform failed with 1 error:\nTests  1 failed (1)"
    cargo_output = (
        "error[E0308]: mismatched types\n"
        "test result: FAILED. 0 passed; 1 failed"
    )

    # User code errors (INTERNALERROR, Transform failed, compile errors) are FAIL.
    assert runner._classify_pytest_status(1, pytest_output) == "FAIL"
    assert runner._classify_vitest_status(1, vitest_output) == "FAIL"
    assert runner._classify_cargo_status(101, cargo_output) == "FAIL"


def test_vitest_filter_uses_exact_reporter_metadata_shapes() -> None:
    runner = _load_runner()
    raw = "\n".join(
        [
            " RUN  v4.1.9 /repo/frontend",
            "   Start at  10:00:00",
            "   Duration  1.00s (tests 500ms)",
            "RUN validation failed",
            "Start at  10:00:00",
            "Duration  1.00s",
        ]
    )

    filtered = runner._filter_vitest_output(raw)

    assert " RUN  v4.1.9 /repo/frontend" not in filtered
    assert "   Start at  10:00:00" not in filtered
    assert "   Duration  1.00s (tests 500ms)" not in filtered
    assert "RUN validation failed" in filtered
    assert "Start at  10:00:00" in filtered
    assert "Duration  1.00s" in filtered


def test_cargo_standard_test_failure_is_not_runner_error() -> None:
    runner = _load_runner()
    output = "\n".join(
        [
            "test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured",
            "error: test failed, to rerun pass `--test sample`",
        ]
    )

    assert runner._classify_cargo_status(101, output) == "FAIL"


def test_cargo_doctest_failure_is_not_runner_error() -> None:
    runner = _load_runner()
    output = "\n".join(
        [
            "test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured",
            "error: doctest failed, to rerun pass `--doc`",
        ]
    )

    assert runner._classify_cargo_status(101, output) == "FAIL"


def test_vitest_filter_preserves_isolated_test_diagnostic() -> None:
    runner = _load_runner()
    raw = "> test\nactual diagnostic context"

    filtered = runner._filter_vitest_output(raw)

    assert "> test" in filtered
    assert "actual diagnostic context" in filtered


def test_pytest_file_not_found_is_not_runner_error() -> None:
    runner = _load_runner()

    output = "ERROR: file or directory not found: /nonexistent/path\n\nno tests ran in 0.00s"
    assert runner._classify_pytest_status(4, output) == "FAIL"


def test_pytest_file_not_found_does_not_return_exit_2() -> None:
    result = _run_runner("--backend", "--", "/nonexistent/path")

    assert result.returncode == 0
    payload = __import__("json").loads(result.stdout)
    assert payload["results"][0]["status"] == "FAIL"


def test_pytest_no_tests_ran_is_not_runner_error() -> None:
    runner = _load_runner()

    output = (
        "ERROR: not found: /repo/src/tests/test_foo.py::test_nonexistent\n"
        "(no match in any of [<Module test_foo.py>])\n\n\n"
        "no tests ran in 0.54s"
    )
    assert runner._classify_pytest_status(4, output) == "FAIL"


def test_pytest_no_tests_ran_does_not_return_exit_2() -> None:
    result = _run_runner(
        "--backend",
        "--",
        "src/tests/test_agent/infrastructure/langgraph/runtime/test_execute_tools_guard.py::test_nonexistent",
    )

    assert result.returncode == 0
    payload = __import__("json").loads(result.stdout)
    assert payload["results"][0]["status"] == "FAIL"
