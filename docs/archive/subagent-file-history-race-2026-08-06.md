# Subagent File-History Race and Tool Isolation

> **Status: Done** — Archived on 2026-08-09.

Date: 2026-08-06


## Goal

Stop implement/subagent runs from dying on concurrent file edits by making session `file-history` snapshots race-safe, and by aligning subagent tool execution with the parent agent's isolation model:

- concurrent `save_file_version()` never raises `FileNotFoundError` on `.tmp -> final` rename;
- same-file concurrent writes in a subagent batch are serialized;
- a single tool exception becomes a tool error result, not a whole-run crash;
- failed subagent finishes persist a durable `error` field in JSONL.

## Current State

Relevant code:

- `src/voidx/tooling/adapters/persistence/file_snapshot.py` — session-scoped pre-write snapshots under `~/.voidx/sessions/<id>/file-history/`
- `src/voidx/tooling/builtin/file/replace.py` / `write.py` / `manage.py` — call `save_file_version()` before mutation
- `src/voidx/agent/infrastructure/langgraph/runtime/tool_executor/helpers.py` — parent batch has per-file rwlock + line-order sort
- `src/voidx/agent/infrastructure/langgraph/runtime/tool_executor/executor.py` — parent wraps tool execution in `try/except` and returns `ToolResult` errors
- `src/voidx/agent/infrastructure/langgraph/runtime/subagent.py` — subagent runs approved tools with bare `asyncio.gather(*[run_one(tc) ...])`
- `src/voidx/agent/infrastructure/langgraph/execution.py` — `subagent_finish` event omits `error` on the normal exception path

Observed failure (session `7c3d2b776f7c`):

```text
[Errno 2] No such file or directory:
'.../file-history/<hash>@vN.tmp' -> '.../file-history/<hash>@vN'
```

Evidence:

- implement runs ended with `finish_reason=error` after parallel `replace` batches;
- some tool_call_ids in the final batch never got a `tool_result` event;
- crashed snapshot names mapped to hot migration files such as `lsp/application/manager.py` and `tooling/adapters/mcp.py`;
- parent tool path already isolates exceptions; subagent path does not.

Root cause chain:

```text
implement batches many replace calls
  -> subagent executes them in parallel without file locks
  -> concurrent save_file_version() for the same path
  -> same version + same tmp name
  -> os.replace(tmp, final) races
  -> FileNotFoundError escapes run_one()
  -> whole subagent run fails
```

## Non-goals

This change does not:

- redesign file-history schema, rollback UX, or manifest format beyond concurrency safety;
- change parent/subagent permission policy or approval flow;
- make all tools strictly serial;
- add cross-process file locks for multiple voidx processes;
- rewrite the broader subagent report protocol (`docs/design/subagent-report-protocol.md`);
- fix unrelated implement quality issues (planning, import migration strategy, etc.).

## Design

### 1. Make `save_file_version()` concurrency-safe

File: `src/voidx/tooling/adapters/persistence/file_snapshot.py`

Invariants after the change:

1. Two concurrent saves for the **same resolved path** never share a snapshot version.
2. Two concurrent saves never share a temporary filename.
3. Snapshot write + manifest append for one save is atomic with respect to other saves in the same process.
4. Callers may still treat snapshot failure as non-fatal only if the implementation chooses to swallow errors; default remains: raise only for unexpected IO after retries are exhausted. Preferred behavior for this fix: **do not raise for recoverable rename races**; either lock so they cannot happen, or retry with a new unique tmp name.

Required implementation:

- Keep a process-local async lock keyed by `session_id` (or by `(session_id, full_hash)`).
- Critical section must cover:
  - read manifest
  - compute next version / snapshot name
  - write snapshot bytes
  - append manifest row
- Temporary file names must be unique even under collision, e.g.:

```text
{snapshot_name}.{pid}.{token}.tmp
```

not:

```text
{snapshot_name}.tmp
```

- Keep existing short-hash collision fallback (`full_hash@vN` when short hash collides).
- Preserve current manifest fields and snapshot content semantics (pre-modification bytes).

Recommended structure:

```python
_SESSION_LOCKS: dict[str, asyncio.Lock] = {}
_SESSION_LOCKS_GUARD = asyncio.Lock()

async def _lock_for_session(session_id: str) -> asyncio.Lock:
    ...

async def save_file_version(...):
    async with await _lock_for_session(ctx.session_id):
        # version allocation + snapshot write + manifest append
```

Session-level lock is acceptable first cut. Per-`(session_id, full_hash)` lock is an allowed optimization if tests prove no manifest read/write hazard.

### 2. Align subagent tool batch isolation with parent

File: `src/voidx/agent/infrastructure/langgraph/runtime/subagent.py`

Parent already:

- extracts file paths from tool calls;
- uses per-path rwlock for `write` / `replace` / `manage`;
- sorts same-file writes by line descending;
- runs non-file tools after file tools when mixed.

Subagent must gain equivalent isolation for the approved batch:

| Rule | Parent today | Subagent target |
|---|---|---|
| Same-file write serialization | yes | yes |
| Same-file write line-order sort | yes | yes |
| Different-file writes parallel | yes | yes |
| Tool exception -> ToolResult error | yes | yes |
| One tool failure kills whole batch/run | no | no |

Implementation options (pick one; prefer reuse):

1. **Preferred:** extract shared batch executor helper from `tool_executor/helpers.py` and call it from both parent and subagent.
2. **Minimal:** copy the file-lock + exception-isolation subset into `subagent.py` without full parent feature parity.

Minimum required behavior in `run_one()`:

```python
try:
    result = await agent_tools.execute_tool(tid, targs, ctx)
except Exception as exc:
    result = ToolResult(
        output=f"Tool execution error: {exc}",
        metadata={"error": True, "exception": exc.__class__.__name__},
    )
```

And batch execution must not use bare:

```python
await asyncio.gather(*[run_one(tc) for tc in approved])
```

without either:

- per-file locks inside `run_one` / wrapper, or
- a shared helper that already serializes same-file writes.

`asyncio.gather(..., return_exceptions=True)` alone is **not** sufficient; exceptions must be converted into tool messages so the LLM can continue.

### 3. Persist subagent finish errors

File: `src/voidx/agent/infrastructure/langgraph/execution.py`

On the exception path, `SubagentFinished` already receives `error=...`, but `append_subagent_event(... subagent_finish ...)` currently omits it.

Target `subagent_finish` payload:

```json
{
  "type": "subagent_finish",
  "agent_id": 3,
  "ok": false,
  "elapsed": 323.7,
  "finish_reason": "error",
  "error": "[Errno 2] No such file or directory: '...@v2.tmp' -> '...@v2'"
}
```

Rules:

- include `error` when `ok` is false and an exception/message exists;
- truncate to a bounded length (existing 500-char pattern is fine);
- do not invent errors on successful finishes.

## File Structure

| File | Responsibility |
|---|---|
| `src/voidx/tooling/adapters/persistence/file_snapshot.py` | Session lock, unique tmp names, race-safe version allocation |
| `src/voidx/agent/infrastructure/langgraph/runtime/subagent.py` | Exception isolation + same-file write serialization for subagent batches |
| `src/voidx/agent/infrastructure/langgraph/runtime/tool_executor/helpers.py` | Optional shared helper extraction for file locks / batch ordering |
| `src/voidx/agent/infrastructure/langgraph/execution.py` | Persist `error` on `subagent_finish` |
| `src/tests/test_tooling/test_file_snapshot_concurrency.py` | New focused concurrency tests for snapshots |
| `src/tests/test_agent_runtime/` or existing subagent runtime tests | Subagent tool exception isolation + same-file parallel replace |

## Acceptance Criteria

1. Concurrent `save_file_version()` on the same path in one process:
   - never raises `FileNotFoundError` from tmp rename;
   - produces strictly increasing versions without duplicate `(full_hash, version)` pairs;
   - leaves no orphan requirement that callers catch rename races.
2. Concurrent `save_file_version()` on different paths still completes successfully and may remain parallel if locks are per-hash; session-global lock is acceptable.
3. Subagent batch with two `replace` calls on the same file:
   - both return tool results (success or structured tool error);
   - does not crash the subagent run with an uncaught exception from snapshot IO.
4. Subagent tool that raises an unexpected exception:
   - yields a tool error message to the model;
   - allows the run to continue or finish cleanly rather than aborting the gather.
5. Failed subagent JSONL contains `error` on `subagent_finish`.
6. Existing file-history consumers keep working:
   - manifest fields unchanged;
   - snapshot files still named `{short_or_full_hash}@v{N}`;
   - only tmp naming becomes unique/internal.

## Test Plan

Prefer `./test.py --backend -- ...`.

### A. Snapshot concurrency

New tests in `src/tests/test_tooling/test_file_snapshot_concurrency.py` (name flexible):

1. `test_concurrent_save_same_path_assigns_unique_versions`
   - create one file and one `ToolContext(session_id=...)`
   - `asyncio.gather` many `save_file_version()` calls
   - assert no exception
   - assert manifest versions are unique and contiguous or at least unique
   - assert every manifest snapshot file exists

2. `test_concurrent_save_different_paths_succeeds`
   - parallel saves across distinct files
   - assert all succeed

3. `test_snapshot_tmp_names_do_not_collide`
   - force overlapping writes (same path)
   - assert final artifacts are only `@vN` files plus `manifest.jsonl` (no leftover requirement beyond success)

### B. Subagent isolation

Add/extend runtime tests covering:

1. monkeypatched tool/`save_file_version` raising once does **not** kill the whole batch;
2. two same-file replaces in one subagent batch are serialized (can assert lock ordering with a controllable fake lock or by checking both tool results exist);
3. finish event/JSONL includes `error` when run fails for other reasons.

If full `run_subagent()` setup is heavy, unit-test the extracted helper and a thin wrapper around `run_one()` exception handling.

### C. Regression commands

```bash
./test.py --backend -- src/tests/test_tooling/test_file_snapshot_concurrency.py -q
./test.py --backend -- src/tests/test_tooling/test_interactive_tools.py -q
./test.py --backend -- src/tests/test_tooling/test_interactive_tools_write.py -q
./test.py --backend -- src/tests/test_infrastructure/runtime/test_session_crud.py -k file_history -q
```

Plus any new subagent runtime test path added by the implementation.

## Implementation Tasks

1. Add failing concurrency tests for `save_file_version()`.
2. Implement session/path lock + unique tmp names in `file_snapshot.py`.
3. Turn tests green for snapshot concurrency.
4. Add failing tests for subagent tool exception isolation / same-file batch safety.
5. Implement subagent batch isolation (shared helper preferred).
6. Persist `error` on `subagent_finish`.
7. Run focused backend tests listed above.

## Risks

| Risk | Mitigation |
|---|---|
| Session-global lock serializes unrelated file snapshots and slows large batches | Accept first; optimize to per-hash lock if needed |
| Extracting shared helper creates large refactor | Prefer minimal subagent-side isolation if extraction is risky |
| Swallowing snapshot errors hides disk-full conditions | Only eliminate rename races; still surface real IO failures as tool errors |
| Tests flake under high concurrency | Use deterministic barriers / enough tasks (e.g. 20+) and assert uniqueness, not wall-clock timing |

## Out-of-scope Follow-ups

- Cross-process file-history locking
- Richer subagent structured report protocol
- Parent/subagent shared workspace write lease unification beyond file-history and same-file tool locks
- Automatic retry of whole implement runs after infrastructure faults

## Decision Summary

| Topic | Decision |
|---|---|
| Primary bug | Concurrent same-path snapshot tmp rename race |
| Why implement fails often | Parallel replace + no subagent file locks + uncaught tool exceptions |
| Snapshot fix | Process-local lock + unique tmp filenames |
| Subagent fix | Exception isolation + same-file write serialization |
| Observability fix | Write `error` into `subagent_finish` JSONL |
| Schema changes | None for manifest/snapshot final names |