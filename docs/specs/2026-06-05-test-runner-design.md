# Test Runner and Result Parsing Design

Date: 2026-06-05

## Goal

Provide a dedicated test execution tool that runs test suites, parses results into structured data, and surfaces failures with precise file/line information. This closes the verification loop — the agent can edit code and immediately validate the changes without manually parsing raw test output.

## Current State

Key files:

- `src/voidx/tools/bash.py` — the only way to run tests. Output is raw text, no structure.
- `src/voidx/tools/lsp.py` — `LspDiagnosticsTool` provides compiler/linter errors, but not test failures.
- `src/voidx/agent/agents.py` — prompts mention "Run the relevant focused tests before broad test runs" but provide no structured tool.

Observed gaps:

- No structured test results — the LLM must parse pytest/jest output as raw text, which is error-prone and token-expensive.
- No test failure → file:line mapping — the agent can't directly navigate to the failing assertion.
- No test discovery — the agent doesn't know what test suites exist without running `find` or `glob`.
- No incremental test running — always runs the full suite or relies on the user to specify the right path.
- No test result caching — re-running the same tests wastes time and tokens.

## External References

- **Claude Code** runs tests via bash but has no dedicated test tool.
- **Cursor** has a test panel that shows pass/fail per test with inline failure details.
- **Aider** automatically runs the test suite after each edit and parses failures to guide fixes.
- **VS Code** test explorer provides structured test results with per-test status, duration, and failure details.

References:

- https://aider.chat/docs/faq.html
- https://code.visualstudio.com/docs/editor/testing

## Design

### Approach: Test Runner Tool with Pluggable Parsers

Add a `test_run` tool that executes test commands and parses output into structured results. Support multiple test frameworks via pluggable parsers, starting with pytest and jest.

### Tool Definition

```python
class TestRunInput(BaseModel):
    command: str | None = Field(
        default=None,
        description=(
            "Test command to run. If omitted, auto-detects based on project files. "
            "Examples: 'pytest tests/test_foo.py', 'npm test -- --grep pattern'"
        )
    )
    framework: str | None = Field(
        default=None,
        description=(
            "Test framework hint: pytest, jest, vitest, go_test, cargo_test. "
            "If omitted, auto-detected from project files."
        )
    )
    paths: list[str] | None = Field(
        default=None,
        description="Specific test files or directories to run."
    )
    focus_failures: bool = Field(
        default=True,
        description="If true, only include detailed output for failed tests in the result."
    )
    timeout: int = Field(
        default=120,
        description="Maximum seconds to wait for test completion."
    )
```

### Structured Output

```python
class TestLocation(BaseModel):
    file_path: str
    line: int | None = None
    test_name: str

class TestFailure(BaseModel):
    test_name: str
    location: TestLocation
    message: str
    expected: str | None = None
    actual: str | None = None
    traceback: str | None = None
    diff: str | None = None          # for assertion diffs

class TestSuiteResult(BaseModel):
    framework: str
    command: str
    exit_code: int
    total: int
    passed: int
    failed: int
    skipped: int
    errors: int                      # collection/setup errors
    duration_ms: int
    failures: list[TestFailure]      # detailed failure info
    raw_output: str | None = None    # included only if focus_failures=False
```

### Auto-Detection

Detect the test framework from project files:

| File | Framework | Default Command |
|------|-----------|----------------|
| `pytest.ini`, `pyproject.toml` with `[tool.pytest]` | pytest | `python -m pytest` |
| `package.json` with `jest` dep | jest | `npx jest` |
| `package.json` with `vitest` dep | vitest | `npx vitest run` |
| `go.mod` | go_test | `go test ./...` |
| `Cargo.toml` | cargo_test | `cargo test` |

Detection logic in `src/voidx/tools/test_detect.py`:

```python
def detect_test_framework(workspace: str) -> tuple[str, str] | None:
    """Returns (framework, default_command) or None."""
    ...
```

### Parsers

Each framework has a dedicated output parser:

#### pytest Parser

Parse pytest's verbose output (`-v --tb=short`):

```
FAILED tests/test_foo.py::test_bar - AssertionError: expected 42
=== short test summary info ===
FAILED tests/test_foo.py::test_bar - AssertionError: expected 42
=== 1 failed, 5 passed in 0.12s ===
```

Also support `--json-report` if `pytest-json-report` is installed for more reliable parsing.

#### jest/vitest Parser

Parse jest's `--verbose` output:

```
FAIL tests/foo.test.ts
  ✕ bar (5ms)
  ● bar
    expect(received).toBe(expected)
    Expected: 42
    Received: 41
```

#### go test Parser

Parse `go test -v` output:

```
--- FAIL: TestBar (0.00s)
    foo_test.go:12: expected 42, got 41
FAIL
```

#### cargo test Parser

Parse `cargo test` output:

```
test bar ... FAILED
failures:
---- bar stdout ----
thread 'bar' panicked at 'assertion failed', src/foo.rs:12:5
```

### Parser Architecture

```python
class TestResultParser(ABC):
    @abstractmethod
    def parse(self, stdout: str, stderr: str, exit_code: int) -> TestSuiteResult: ...

class PytestParser(TestResultParser): ...
class JestParser(TestResultParser): ...
class VitestParser(TestResultParser): ...
class GoTestParser(TestResultParser): ...
class CargoTestParser(TestResultParser): ...

def get_parser(framework: str) -> TestResultParser:
    return _PARSERS[framework]
```

### Smart Test Selection

When the agent edits a file, suggest running only the related tests:

```python
def suggest_test_paths(edited_file: str, workspace: str) -> list[str]:
    """Given an edited source file, find likely test files."""
    # Convention-based: test_foo.py for foo.py, foo.test.ts for foo.ts
    # Also check for test directories that reference the file
    ...
```

This is heuristic-based (convention matching), not AST-based. Good enough for most projects.

### Integration with Edit Workflow

The agent prompt should include a verification step:

```
After making code changes:
1. Run the relevant focused tests first.
2. If tests fail, read the failure details and fix.
3. If tests pass, run the broader test suite.
```

The `test_run` tool makes step 1-2 efficient by providing structured failure data instead of raw text.

### LSP Diagnostics Synergy

After running tests, also check LSP diagnostics on modified files:

```
1. test_run → get failures
2. lsp_diagnostics → get type errors, lint issues
3. Combine both into a complete verification result
```

This is a prompt-level integration, not a code change — the agent naturally chains these tools.

### Caching

Cache test results for the current session:

```python
class TestResultCache:
    """Cache test results keyed by (command, file_mtimes_snapshot)."""
    def get(self, key: str) -> TestSuiteResult | None: ...
    def set(self, key: str, result: TestSuiteResult, ttl: float = 300.0) -> None: ...
```

If the same test command is run again and no source files have changed, return the cached result. This prevents wasting time on redundant runs within a session.

### Permission

- `test_run` is classified as `BASH_READ` capability (tests are read-only operations on the codebase).
- Default action: `allow` — tests don't modify source files.
- However, the underlying command execution uses the same subprocess mechanism as bash, so sandbox checks apply to any file writes the test itself might do.

## Scope

In scope:

- `test_run` tool with auto-detection and 5 framework parsers.
- Structured `TestSuiteResult` output model.
- Smart test path suggestion.
- Test result caching.
- Permission integration.

Out of scope:

- Test coverage collection and display (future — needs coverage parser).
- Test generation (the agent can write tests via `edit`/`write` tools).
- Visual test result UI in TUI/Web (future).
- Watch mode / continuous test running.
- Test debugging (breakpoints, step-through).

## File Changes

| File | Change |
|------|--------|
| `src/voidx/tools/test_run.py` | New — `TestRunTool`, `TestRunInput`, execution logic |
| `src/voidx/tools/test_detect.py` | New — framework auto-detection |
| `src/voidx/tools/test_parser.py` | New — `TestResultParser` base + 5 framework parsers |
| `src/voidx/tools/test_cache.py` | New — `TestResultCache` |
| `src/voidx/tools/registry.py` | Register `TestRunTool` |
| `src/voidx/permission/engine.py` | Add `test_run` to `BASIC_RULES` (action=allow) |
| `src/voidx/agent/agents.py` | Update prompts to mention `test_run` tool |
| `tests/test_tools/test_test_run.py` | New — parser tests, detection tests, execution tests |

## Risks

| Risk | Mitigation |
|------|-----------|
| Parser breaks on unusual test output | Fallback to raw text with parse error metadata; parsers are per-framework and narrow in scope |
| Test commands have side effects (DB writes, file creation) | Sandbox applies; document that `test_run` is for unit/integration tests, not destructive E2E |
| Auto-detection picks wrong framework | Allow `framework` override; show detected framework in result |
| Long-running tests exceed timeout | Configurable timeout; return partial results with "timed out" status |
| Test output is very large (thousands of lines) | `focus_failures=True` by default; truncate raw output to 5000 chars |
| Framework not supported | Return raw output with `framework="unknown"`; user can specify command manually |
