# Test Runner and Result Parsing Design

Date: 2026-06-05

> **Status: Deferred** - document-only update for this cycle. Do not implement in
> the current release cycle.

## Goal

Provide a dedicated test execution tool that runs focused test suites, parses the
result into structured data, and surfaces failures with file/line information.
The tool should reduce raw-output parsing by the agent while preserving the same
safety posture as shell-based test execution.

The first implementable version must be intentionally narrow: pytest-first,
permission-gated, no arbitrary shell command, no result cache, and no broad
multi-framework parser matrix.

## Current State

Key files:

- `src/voidx/tools/bash.py` - current way to run tests. It returns raw stdout,
  stderr, and exit code.
- `src/voidx/permission/rules.py` - classifies `python -m pytest` as
  `BASH_WRITE`, because tests execute project code and may write cache, coverage,
  snapshots, temp files, or fixtures.
- `src/voidx/permission/engine.py` - denies write-capability commands in plan
  mode and read-only sandbox mode.
- `src/voidx/tools/lsp.py` - provides compiler/linter diagnostics, not test
  failures.

Observed gaps:

- Test results are unstructured, so the agent parses text manually.
- Failure locations are not normalized into `file_path` and `line`.
- Focused test commands are easy to mistype or over-broaden.
- Raw output can be large and token-expensive.

Important existing constraint:

- Test execution is not a read-only operation. A `test_run` tool must not be
  granted by reclassifying test execution as `BASH_READ`.

## Design Summary

Add a future `test_run` tool that runs pytest through controlled argv
construction and returns structured JSON. The tool is not a general shell
runner. It does not accept arbitrary `command: str` in V1.

V1 supports:

- pytest detection and execution only;
- explicit path-scoped test selection;
- structured pass/fail/error/skip totals;
- bounded raw output fallback;
- timeout handling;
- permission integration through a dedicated test capability.

V1 does not support:

- jest, vitest, Go, or Cargo parsers;
- arbitrary shell commands;
- result caching;
- smart test selection from edited source files;
- coverage parsing;
- UI test panels or watch mode.

## Tool Input

```python
class TestRunInput(BaseModel):
    framework: Literal["pytest"] | None = Field(
        default=None,
        description="Test framework. V1 supports pytest only; omitted means auto-detect pytest."
    )
    paths: list[str] = Field(
        default_factory=list,
        description="Workspace-relative test files or directories to run."
    )
    keyword: str = Field(
        default="",
        description="Optional pytest -k expression."
    )
    marker: str = Field(
        default="",
        description="Optional pytest -m marker expression."
    )
    maxfail: int | None = Field(
        default=None,
        ge=1,
        le=100,
        description="Optional pytest --maxfail value."
    )
    last_failed: bool = Field(
        default=False,
        description="Run pytest --lf."
    )
    verbose: bool = Field(
        default=False,
        description="Use -v instead of -q."
    )
    include_raw_output: bool = Field(
        default=False,
        description="Include bounded raw output in the structured result."
    )
    timeout: int = Field(
        default=120,
        ge=1,
        le=1800,
        description="Maximum seconds to wait for test completion."
    )
```

Rationale:

- `paths`, `keyword`, `marker`, `maxfail`, and `last_failed` cover common
  focused pytest workflows without opening a shell escape hatch.
- The tool builds argv with `asyncio.create_subprocess_exec`, not a shell.
- Any future free-form argument support must be allowlisted and reviewed.

## Structured Output

```python
class TestLocation(BaseModel):
    file_path: str
    line: int | None = None
    test_name: str = ""

class TestFailure(BaseModel):
    test_name: str
    location: TestLocation
    message: str
    traceback: str = ""
    diff: str = ""

class TestSuiteResult(BaseModel):
    ok: bool
    framework: str
    command_argv: list[str]
    exit_code: int
    timed_out: bool
    duration_ms: int
    total: int
    passed: int
    failed: int
    skipped: int
    errors: int
    failures: list[TestFailure]
    raw_output: str = ""
    raw_output_truncated: bool = False
    parse_error: str = ""
```

`ok` is true only when the process exits with code `0` and does not time out.
Parser failure must not hide test failure. If parsing fails, return the exit
code, bounded raw output, and `parse_error`.

## Pytest Detection

V1 detects pytest only at the workspace root:

1. If `pyproject.toml` contains `[tool.pytest.ini_options]`, use pytest.
2. Else if `pytest.ini`, `tox.ini`, or `setup.cfg` contains pytest config, use
   pytest.
3. Else if `tests/` exists, pytest may still be selected when the caller
   explicitly sets `framework="pytest"`.

Default executable:

1. Use `<workspace>/.venv/bin/python -m pytest` when that interpreter exists.
2. Otherwise use `python -m pytest`.

The current project convention remains compatible with this default because its
documented command is `.venv/bin/python -m pytest tests/ -v`.

## Command Construction

The tool constructs argv as data:

```python
argv = [python_executable, "-m", "pytest"]
argv.extend(validated_paths)
argv.extend(["-v" if inp.verbose else "-q", "--tb=short", "-rA"])
if inp.keyword:
    argv.extend(["-k", inp.keyword])
if inp.marker:
    argv.extend(["-m", inp.marker])
if inp.maxfail:
    argv.extend(["--maxfail", str(inp.maxfail)])
if inp.last_failed:
    argv.append("--lf")
```

Path handling:

- Every path is resolved with `resolve_safe`.
- Paths must stay inside the workspace.
- Missing paths return a structured tool error before running pytest.
- An empty path list means run pytest's configured default selection.

Execution handling:

- Use `asyncio.create_subprocess_exec`.
- Use `stdin=DEVNULL`.
- Run with `cwd=ctx.workspace`.
- Terminate the process group on timeout, matching the bash tool's behavior.
- Cap captured stdout/stderr before serializing result output.

## Parser

V1 implements `PytestParser` only.

Input:

- stdout;
- stderr;
- exit code;
- duration.

Required parsing:

- final summary counts, such as `1 failed, 5 passed in 0.12s`;
- short summary entries, such as
  `FAILED tests/test_foo.py::test_bar - AssertionError: expected 42`;
- location hints from traceback lines like `tests/test_foo.py:12`;
- collection/setup errors as `errors`.

The parser should be conservative. If a field cannot be derived reliably, leave
it empty rather than inventing data. The raw output fallback is the safety net.

`pytest-json-report` is not required in V1. A later version may detect and use
it when already installed, but V1 must not add a dependency or depend on the
plugin for correctness.

## Permission Model

Add a dedicated capability:

```python
class PermissionCapability(str, Enum):
    ...
    TEST_RUN = "test_run"
```

Rules:

- `test_run` maps to `PermissionCapability.TEST_RUN`.
- `BASIC_RULES` uses `Rule(permission="test_run", pattern="*", action="ask")`.
- Default strategy asks before running tests.
- Plan mode denies `TEST_RUN`.
- Read-only sandbox denies `TEST_RUN`.
- `approval_policy=on-failure` treats `TEST_RUN` like `BASH_WRITE`: ask first,
  do not run automatically as a failure check.
- `danger-full-access` with `approval_policy=never` may allow it through the
  existing approval policy path.

Reasoning:

- Tests execute arbitrary repository code.
- Tests may write files under the workspace.
- A dedicated capability avoids falsely treating tests as read-only while still
  allowing future UX to distinguish test execution from general shell commands.

## No Result Cache In V1

Do not cache test results in V1.

Rationale:

- Verification must reflect a fresh run.
- Test outcomes can depend on environment variables, generated files, time,
  network services, dependency state, and test order.
- Returning stale pass results would undermine the purpose of the tool.

Future versions may cache discovery metadata, but result caching must be opt-in
and must surface `cached: true` in output.

## Future Work

These are intentionally out of V1:

- jest/vitest/go/cargo execution and parsers;
- result caching;
- smart test selection from edited source files;
- coverage collection;
- frontend or TUI test result panels;
- watch mode;
- debugger integration;
- free-form command execution.

Any future multi-framework support should follow the same pattern:

- controlled argv construction;
- framework-specific permission assumptions reviewed explicitly;
- bounded output;
- structured parser with raw fallback;
- tests for path safety and permission behavior.

## File Changes For Future Implementation

| File | Change |
|------|--------|
| `src/voidx/tools/test_run.py` | New `TestRunTool`, input/output models, argv construction, execution |
| `src/voidx/tools/test_detect.py` | New pytest-only detection helper |
| `src/voidx/tools/test_parser.py` | New parser base plus `PytestParser` |
| `src/voidx/tools/registry.py` | Register `TestRunTool` |
| `src/voidx/permission/rules.py` | Add `TEST_RUN` capability and classification |
| `src/voidx/permission/engine.py` | Deny in plan/read-only; ask by default and on-failure |
| `tests/test_tools/test_test_run.py` | Detection, argv, parser, timeout, path safety, output cap |
| `tests/test_agent/test_permission.py` | Permission classification and mode overlay coverage |
| `tests/test_tools/test_basic.py` | Registry coverage |

No implementation is planned for the current cycle.

## Test Plan For Future Implementation

| Test | Purpose |
|------|---------|
| `test_test_run_detects_pytest_from_pyproject` | Detect pytest config in this repository style |
| `test_test_run_uses_venv_python_when_available` | Prefer `.venv/bin/python -m pytest` |
| `test_test_run_rejects_outside_workspace_path` | Block path traversal |
| `test_test_run_rejects_missing_path` | Fail before spawning pytest |
| `test_test_run_constructs_argv_without_shell` | Ensure no shell command string is used |
| `test_test_run_parses_passing_pytest_summary` | Structured pass counts |
| `test_test_run_parses_failed_pytest_summary` | Failure name, message, file, and line |
| `test_test_run_parses_collection_error` | Collection/setup errors become `errors` |
| `test_test_run_returns_raw_fallback_on_parse_error` | Parser failure does not hide process result |
| `test_test_run_times_out_and_terminates_process` | Timeout behavior is structured |
| `test_test_run_caps_raw_output` | Prevent unbounded tool output |
| `test_permission_classifies_test_run_as_test_run` | Dedicated capability |
| `test_permission_test_run_asks_by_default` | Default safety posture |
| `test_permission_test_run_denied_in_plan_mode` | Plan mode cannot run tests |
| `test_permission_test_run_denied_in_read_only_sandbox` | Read-only sandbox cannot run tests |

## Risks

| Risk | Mitigation |
|------|------------|
| `test_run` becomes a shell bypass | Do not accept arbitrary command strings; construct argv only |
| Tests write files or mutate local state | Treat as `TEST_RUN`, ask by default, deny in read-only and plan mode |
| Parser misses unusual pytest output | Return bounded raw output and `parse_error` |
| Auto-detection chooses wrong command | V1 only detects pytest at workspace root; allow explicit `framework="pytest"` |
| Large output overwhelms context | Cap raw output and include failure-focused details |
| Tool produces stale verification | No result cache in V1 |
| Scope grows before safety is proven | Defer multi-framework support and smart selection to future versions |

## Completion Criteria

This design can be archived only after:

- the pytest-only `test_run` tool exists;
- permissions and sandbox behavior are covered by tests;
- parser and execution tests exist;
- focused and full test suites pass;
- no arbitrary command execution path is introduced.
