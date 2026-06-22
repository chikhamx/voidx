# Structured Output for bash, grep, glob

> **Status: Done**

## Problem

`bash`, `grep`, `glob` return free-form text in `ToolResult.output`. The LLM must parse this text to reason about results (e.g., "did the command succeed?", "which files matched?"). This is fragile and wastes tokens.

Meanwhile, `git` and `workflow` already return JSON in `output` via `json.dumps(payload)`, and `metadata` is used by the MCP server as `structuredContent`. The pattern exists but isn't applied consistently.

## Current State

| Tool | `output` format | `metadata` |
|------|----------------|------------|
| bash | stdout+stderr text | `{command, exit_code, stdout_size, stderr_size}` |
| grep | `file:line:content` lines | `{pattern, matches, match_details, truncated}` |
| glob | newline-separated paths | `{pattern, matches, truncated}` |
| git | **JSON** via `_result()` | `{command, ok, error}` |
| workflow | **JSON** via `_success()` | `{workflow_transition, state_patch}` |

Key insight: `result.output` → `sanitize_tool_message_content()` → `ToolMessage.content` → LLM. The LLM sees `output` directly. `metadata` is only consumed by the MCP server bridge.

## Design

### Principle

Follow the `git` pattern: `output` = `json.dumps(payload)` with a structured dict. Keep `metadata` for machine consumers (MCP, UI). The LLM gets parseable JSON; the UI gets the same data via metadata.

### 1. BashTool

**Before:**
```
output = "hello\n[stderr]\nwarning"
metadata = {"command": "echo hello", "exit_code": 0, "stdout_size": 6, "stderr_size": 7}
```

**After:**
```python
payload = {
    "ok": exit_code == 0,
    "exit_code": exit_code,
    "stdout": stdout_text,
    "stderr": stderr_text,
}
output = json.dumps(payload, ensure_ascii=False)
metadata = {"command": inp.command, "exit_code": exit_code, "ok": exit_code == 0}
```

LLM sees:
```json
{"ok": true, "exit_code": 0, "stdout": "hello\n", "stderr": ""}
```

**Edge cases:**
- Timeout: `{"ok": false, "exit_code": -1, "stdout": "", "stderr": "", "timeout": true}`
- Blocked: `{"ok": false, "exit_code": -1, "stdout": "", "stderr": "Blocked: ...", "blocked": true}`
- Route hint: unchanged (already structured in metadata, output is guidance text)

### 2. GrepTool

**Before:**
```
output = "src/main.py:42:def foo()\nsrc/util.py:10:import os"
metadata = {"pattern": "foo", "matches": 2, "match_details": [...], "truncated": false}
```

**After:**
```python
payload = {
    "pattern": inp.pattern,
    "matches": count,
    "truncated": truncated,
    "results": match_details,  # [{file, line, column, content}, ...]
}
output = json.dumps(payload, ensure_ascii=False)
metadata = {"pattern": inp.pattern, "matches": count, "truncated": truncated}
```

LLM sees:
```json
{"pattern": "foo", "matches": 2, "truncated": false, "results": [
  {"file": "src/main.py", "line": 42, "column": 5, "content": "def foo()"},
  {"file": "src/util.py", "line": 10, "column": 1, "content": "import os"}
]}
```

### 3. GlobTool

**Before:**
```
output = "a.py\nsub/b.py"
metadata = {"pattern": "**/*.py", "matches": 2, "truncated": false}
```

**After:**
```python
payload = {
    "pattern": inp.pattern,
    "matches": total,
    "truncated": total > 200,
    "files": shown,  # list[str], already computed
}
output = json.dumps(payload, ensure_ascii=False)
metadata = {"pattern": inp.pattern, "matches": total, "truncated": total > 200}
```

LLM sees:
```json
{"pattern": "**/*.py", "matches": 2, "truncated": false, "files": ["a.py", "sub/b.py"]}
```

## Impact Analysis

### LLM prompt consumption
- `result.output` → `ToolMessage.content` → LLM. JSON is more parseable than free text.
- Token cost: JSON adds ~10-20% overhead (keys, braces) vs raw text. Acceptable trade-off for structured data.

### UI display
- `notify_tool_text_output` sends `result.output` to the TUI/events. Currently renders as plain text.
- After change: TUI will show JSON text. This is a **regression for human readability**.

**Mitigation:** Add a `display: str = ""` field to `ToolResult`. When set, UI uses `display` for rendering; LLM still sees `output`. When empty, UI falls back to `output` (backward compatible).

### Other consumers of `result.output`
- **Subagent** (`subagent.py:275,281,284`): Uses `result.output` for child agent results. Bash/grep/glob results are not typically forwarded through subagent, but the `display` field ensures backward compat if they are.
- **Permission preview** (`permissions.py:127`): Takes `result.output[:200]` for error preview. JSON prefix is still readable.
- **MCP server** (`mcp_servers/web.py:61`): Uses `result.output` as text content. Bash/grep/glob are not exposed via MCP, no impact.

### Tool result persistence
- `maybe_persist_tool_result` truncates at 50K chars. JSON output is slightly larger but well within limits.

### Test impact
- Tests assert on `result.output` containing text fragments. Need updating to parse JSON or assert on `result.metadata` / `result.display`.

## Changes

### `src/voidx/tools/base.py`
- Add `display: str = ""` to `ToolResult`

### `src/voidx/agent/graph/tool_executor/executor.py`
- Line 207: use `result.display or result.output` for UI text output (instead of `result.output`)

### `src/voidx/tools/bash.py`
- `execute()`: build JSON payload, set `output = json.dumps(payload)`, set `display` to human-readable format (current output style)

### `src/voidx/tools/search.py`
- `GlobTool.execute()`: build JSON payload with `files` list, set `display` to newline-separated paths
- `GrepTool.execute()`: build JSON payload with `results` list, set `display` to `file:line:content` format

### Tests
- Update `tests/test_tools/test_bash_tool.py`: parse JSON output or assert on metadata/display
- Update `tests/test_tools/test_search.py`: same pattern
