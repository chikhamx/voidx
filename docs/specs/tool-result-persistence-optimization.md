# Tool Result Persistence Optimization

## Status
Spec — awaiting implementation.

## Problem

Tool results between 4,000 and 50,000 chars are silently lost:

1. `maybe_persist_tool_result` (threshold 50,000) considers them "not large enough" → does not persist to disk.
2. `sanitize_tool_message_content` (threshold 4,000) hard-truncates them before sending to the LLM.
3. The truncated portion (4,000–50,000 chars) is gone — no disk file, no recovery path.

Additionally, all persisted tool-result files are stored under the global `~/.voidx/tool-results/` directory. There is no workspace-level isolation, making it harder for users to inspect, archive, or clean up tool results per-project.

## Goal

1. **Eliminate the content-loss gap**: align the persistence threshold with the sanitize truncation threshold so any content that would be truncated is always recoverable from disk.
2. **Support workspace-level tool-result storage**: persist to `<workspace>/.voidx/tool-results/` when a workspace is available, falling back to the global `~/.voidx/tool-results/` when not. Both paths coexist; workspace-level is preferred.

## Design

### Threshold alignment

- Import `DEFAULT_TOOL_MESSAGE_MAX_CHARS` (4,000) from `voidx.agent.tool_messages` into `tool_result_storage.py`.
- Set `TOOL_RESULT_PERSIST_THRESHOLD` to `DEFAULT_TOOL_MESSAGE_MAX_CHARS` so that any output that would be truncated by `sanitize_tool_message_content` is already persisted to disk.
- The `read` tool remains exempt — it has its own pagination mechanism (`offset`/`limit`) and never triggers persistence.

### Workspace-level storage path

- `maybe_persist_tool_result` and `cleanup_session_results` accept an optional `workspace: str | None = None` parameter.
- When `workspace` is provided, files are stored under `voidx_workspace_dir(workspace) / "tool-results" / session_id / {tool_use_id}.txt`.
- When `workspace` is `None`, fall back to the existing global path `DATA_DIR / "tool-results" / session_id / {tool_use_id}.txt`.
- `cleanup_session_results` cleans both workspace-level and global-level directories for the given session.

### Caller updates

- `executor.py` line 309: pass `workspace=ctx.workspace` to `maybe_persist_tool_result`.
- `subagent.py` line 364: insert `maybe_persist_tool_result` call before `sanitize_tool_message_content` — subagent tool results currently go directly to sanitize without persistence, causing the same content-loss gap.
- `session_runtime.py` line 101: pass `workspace=host._workspace` to `cleanup_session_results`.

### `_persist_to_disk` signature change

- `_persist_to_disk` accepts `workspace: str | None = None` parameter and branches the storage path accordingly.
- This function is imported by tests, so the signature change must be backward compatible (default `None` → global path).

### Workspace path resolution fallback

- If `voidx_workspace_dir(workspace)` raises `OSError` (e.g. unresolvable path), fall back to the global `DATA_DIR / "tool-results"` path.
- This ensures persistence never fails due to workspace path issues.

### Preview ratio

- `TOOL_RESULT_PREVIEW_CHARS` stays at 2,000. With threshold at 4,000, the preview is 50% of the full content — this is intentional, as the preview's purpose is to give the LLM enough context to decide whether to read the full file.

## Source Paths

| File | Responsibility |
|---|---|
| `src/voidx/agent/tool_result_storage.py` | Threshold constant, persistence logic, workspace path support, cleanup |
| `src/voidx/agent/tool_messages.py` | `DEFAULT_TOOL_MESSAGE_MAX_CHARS` constant (source of truth for truncate threshold) |
| `src/voidx/agent/graph/tool_executor/executor.py` | Caller — passes workspace to `maybe_persist_tool_result` |
| `src/voidx/agent/graph/subagent.py` | Caller — needs `maybe_persist_tool_result` before `sanitize_tool_message_content` at line 364 |
| `src/voidx/agent/graph/session_runtime.py` | Caller — passes workspace to `cleanup_session_results` |
| `src/voidx/paths.py` | `voidx_workspace_dir()` helper (already exists) |
| `src/tests/test_agent/test_tool_result_storage.py` | Tests for persistence, preview, cleanup |

## Current Behavior

- `TOOL_RESULT_PERSIST_THRESHOLD = 50_000` — only outputs > 50,000 chars are persisted.
- `DEFAULT_TOOL_MESSAGE_MAX_CHARS = 4_000` — outputs > 4,000 chars are hard-truncated before LLM delivery.
- Persistence path: `~/.voidx/tool-results/{session_id}/{tool_use_id}.txt` (global only).
- `read` tool is exempt from persistence (has own pagination).
- `cleanup_session_results(session_id)` cleans global directory only.

## Target Behavior

- `TOOL_RESULT_PERSIST_THRESHOLD = DEFAULT_TOOL_MESSAGE_MAX_CHARS` (4,000) — any output that would be truncated is persisted.
- Persistence path: `<workspace>/.voidx/tool-results/{session_id}/{tool_use_id}.txt` when workspace is available; `~/.voidx/tool-results/{session_id}/{tool_use_id}.txt` as fallback.
- `read` tool remains exempt.
- `cleanup_session_results(session_id, workspace=...)` cleans both workspace-level and global-level directories.
- LLM can always recover truncated content by calling `read` on the persisted file path.

## Constraints

- Do not change `DEFAULT_TOOL_MESSAGE_MAX_CHARS` (4,000) — it is referenced by many tools and tests.
- Do not change the `read` tool exemption.
- Do not change the preview format (`<persisted-output>` block) — LLM and tests already depend on it.
- Do not change `sanitize_tool_message_content` behavior — it still truncates at 4,000 chars, but now the full content is always on disk.
- Backward compatible: `workspace` parameter defaults to `None`, so existing callers without the parameter continue to use the global path.

## Forbidden Changes

- Do not remove or rename `TOOL_RESULT_PERSIST_THRESHOLD` — it is imported by tests.
- Do not change `voidx_workspace_dir` or `voidx_home` in `paths.py`.
- Do not modify the `read` tool's pagination logic.

## Risks

- Disk file proliferation: threshold drop from 50,000 to 4,000 means ~12x more files. Mitigated by `cleanup_session_results` on session end, but orphaned files from crashed sessions have no automatic cleanup.
- Performance: additional disk I/O for medium-sized outputs (4,000+ chars). Write latency for a 4KB file is negligible on modern SSDs.

## Test Commands

```bash
# Run tool result storage tests
./test.py --backend -- src/tests/test_agent/test_tool_result_storage.py -v

# Run tool execution tests
./test.py --backend -- src/tests/test_agent/graph/test_tool_result_preview.py -v

# Run broader agent tests
./test.py --backend -- src/tests/test_agent/ -v
```

## Acceptance Criteria

1. An output of 5,000 chars (above 4,000, below old 50,000) is persisted to disk and the LLM receives a `<persisted-output>` block with file path and preview.
2. Boundary: output of exactly 4,000 chars is NOT persisted (≤ threshold); output of 4,001 chars IS persisted.
3. The persisted file is located under `<workspace>/.voidx/tool-results/{session_id}/` when workspace is provided.
4. When workspace is `None`, the persisted file is under `~/.voidx/tool-results/{session_id}/` (backward compatible).
5. When workspace path resolution fails (`OSError`), persistence falls back to the global `~/.voidx/tool-results/` path.
6. `cleanup_session_results` with workspace parameter cleans the workspace-level directory.
7. `cleanup_session_results` without workspace parameter cleans the global-level directory (backward compatible).
8. `cleanup_session_results` with workspace parameter also cleans the global-level directory (dual-path cleanup).
9. The `read` tool is still exempt from persistence.
10. Subagent tool results (`subagent.py:364`) are persisted before sanitize — no content loss in subagent path.
11. All existing tests pass after updating threshold-dependent assertions.
