# Log Migration: Standard logging → voidx JSONL system

> **Status: Done** — Archived on 2026-07-10.

**Date:** 2026-07-10  
**Status:** Draft / Awaiting approval

## Goal

Migrate all meaningful `log.debug/info/warning/exception` calls in `src/voidx/` to the existing JSONL logging system (`log_tool_event` / `log_internal_error` / `log_llm_diagnostic`), and delete calls that have no diagnostic value.

## Architecture

The codebase has two disjoint logging paths: (1) standard `logging.getLogger(...)` calls (40+ locations, no handlers configured, warning+ only goes to bare stderr, debug/info silent), and (2) `voidx.logging` functions that write structured JSONL to `~/.voidx/logs/`. This plan collapses path (1) into path (2) for everything that has diagnostic value, and removes the rest. The three `logging/` module self-error warnings (`logger.warning("Failed to write ...")`) are kept as bare stderr fallbacks — they are the last resort when JSONL itself fails.

## Event naming convention

Existing convention: `{domain}_{event_name}` (snake_case). New events follow the same pattern:

| Domain prefix | Examples |
|---|---|
| `mcp_` | `mcp_connected`, `mcp_stopped`, `mcp_stderr`, `mcp_protocol_mismatch` |
| `lsp_` | `lsp_read_loop_failed` |
| `ui_` | `ui_clarify_orphan`, `ui_checkpoint_orphan`, `ui_status_orphan` |
| `tool_` | `tool_retry_failed`, `tool_grep_read_failed`, `tool_gitignore_parse_failed` |
| `model_` | `model_catalog_fetch_failed` |

## Tech stack

No new dependencies. Uses existing `voidx.logging.tool_log.log_tool_event`, `voidx.logging.internal_error.log_internal_error`, and `voidx.logging.request_log.log_llm_diagnostic`.

## Files to modify

| # | File | Action |
|---|---|---|
| 1 | `src/voidx/mcp/client/base.py` | Replace 5 log calls → `log_tool_event` / remove |
| 2 | `src/voidx/mcp/client/http_transport.py` | Replace 2 log.warning → `log_tool_event` |
| 3 | `src/voidx/mcp/client/stdio_transport.py` | Replace 3 log calls → `log_tool_event` |
| 4 | `src/voidx/mcp/client/sse_transport.py` | Replace 4 log calls → `log_tool_event` |
| 5 | `src/voidx/mcp/manager.py` | Replace 6 log calls → `log_tool_event` / `log_internal_error` |
| 6 | `src/voidx/lsp/client.py` | Replace 1 log.debug → `log_tool_event` |
| 7 | `src/voidx/memory/transcript.py` | **Delete** 2 log.debug |
| 8 | `src/voidx/ui/output/browse.py` | **Delete** 2 _logger.debug |
| 9 | `src/voidx/ui/output/events/bus.py` | Remove redundant logger.warning (L103), keep log_tool_event |
| 10 | `src/voidx/ui/output/events/consumers.py` | Replace 3 inline logging.warning → `log_internal_error` |
| 11 | `src/voidx/ui/output/dock/nodes_clarify.py` | Replace 1 logger.debug → `log_tool_event` |
| 12 | `src/voidx/ui/output/dock/nodes_checkpoint.py` | Replace 1 logger.debug → `log_tool_event` |
| 13 | `src/voidx/ui/output/dock/nodes_status.py` | Replace 1 inline logging.debug → `log_tool_event` |
| 14 | `src/voidx/ui/gateway/server.py` | Remove 2 redundant logger.warning (L61, L112); replace 1 logger.exception → `log_internal_error` |
| 15 | `src/voidx/tools/retry.py` | Remove unused `logger` param + dead log line |
| 16 | `src/voidx/tools/search.py` | Replace 2 _logger.debug → `log_tool_event` (already has log_tool_event alongside, consolidate) |
| 17 | `src/voidx/llm/catalog.py` | Replace 2 _logger.debug → `log_tool_event` |
| 18 | `src/voidx/llm/instruction.py` | Replace 1 logger.debug → `log_llm_diagnostic` (kept behind `_debug` flag) |
| 19 | `src/voidx/selfupdate.py` | **Delete** 1 logger.debug |

## Tasks

### Phase 0 — Preparation

- [ ] **0.1** Confirm all existing JSONL logging functions work: run `./test.py --backend -- src/tests/` to establish baseline green.

### Phase 1 — MCP layer (most valuable)

- [ ] **1.1** `src/voidx/mcp/client/base.py`:
  - Replace L130 `log.info("MCP client '%s' connected (%s)", ...)` → `log_tool_event("mcp_connected", tool_name=..., message=...)`
  - Replace L152 `log.info("MCP client '%s' stopped", ...)` → `log_tool_event("mcp_stopped", ...)`
  - Replace L224 `log.warning(` (protocol mismatch) → `log_tool_event("mcp_protocol_mismatch", ...)`
  - Replace L284 `log.warning("Failed to send notification...")` → `log_tool_event("mcp_notification_failed", ...)`
  - **Delete** L322 `log.debug("MCP notification from...")` (pure dev noise)
  - Remove `import logging` and `log = logging.getLogger(__name__)` if no remaining calls

- [ ] **1.2** `src/voidx/mcp/client/http_transport.py`:
  - Replace L34 `log.warning(` → `log_tool_event("mcp_auth_deprecated", ...)`
  - Replace L91 `log.warning("Invalid SSE JSON...")` → `log_tool_event("mcp_invalid_json", ...)`
  - Remove `import logging` / `log = ...` if no remaining calls

- [ ] **1.3** `src/voidx/mcp/client/stdio_transport.py`:
  - Replace L85 `log.warning("Invalid JSON...")` → `log_tool_event("mcp_invalid_json", ...)`
  - Replace L91 `log.debug("stdio reader for '%s' exited...")` → `log_tool_event("mcp_stdio_exited", ...)`
  - Replace L114 `log.debug("[MCP stderr:%s] %s", ...)` → `log_tool_event("mcp_stderr", ...)` (valuable debugging data)
  - Remove `import logging` / `log = ...` if clean

- [ ] **1.4** `src/voidx/mcp/client/sse_transport.py`:
  - Replace L38 `log.warning(` → `log_tool_event("mcp_auth_deprecated", ...)`
  - Replace L124 `log.info("MCP SSE '%s': endpoint = %s")` → `log_tool_event("mcp_sse_endpoint", ...)`
  - Replace L137 `log.warning("Invalid SSE JSON...")` → `log_tool_event("mcp_invalid_json", ...)`
  - Replace L144 `log.debug("SSE reader for '%s' exited...")` → `log_tool_event("mcp_sse_exited", ...)`
  - Remove `import logging` / `log = ...` if clean

- [ ] **1.5** `src/voidx/mcp/manager.py`:
  - Replace L75 `log.info("Starting %d MCP server(s)...")` → `log_tool_event("mcp_start_all", ...)`
  - Replace L91 `log.warning("...did not complete within %.0fs")` → `log_tool_event("mcp_init_timeout", ...)`
  - Replace L103 `log.warning("Could not list tools from...")` → `log_tool_event("mcp_list_tools_failed", ...)`
  - Replace L136 `log.info("...%d tools registered")` → `log_tool_event("mcp_tools_registered", ...)`
  - Replace L163 `log.info("Stopping %d MCP server(s)...")` → `log_tool_event("mcp_stop_all", ...)`
  - Replace L260 `log.warning("MCP server '%s' failed to start: %s", ...)` → `log_internal_error(exc, context="mcp_server_start")` (has exception object `e`)
  - Replace L279 `log.exception("Error stopping MCP server '%s'", ...)` → `log_internal_error(exc, context="mcp_server_stop")`

### Phase 2 — LSP

- [ ] **2.1** `src/voidx/lsp/client.py`:
  - Replace L204 `log.debug("LSP read loop failed for %s: %s", ...)` → `log_tool_event("lsp_read_loop_failed", ...)`

### Phase 3 — UI layer

- [ ] **3.1** `src/voidx/ui/output/events/bus.py`:
  - L103 `logger.warning(message)` is **redundant** — L104 immediately calls `log_tool_event(...)`. Delete L103.

- [ ] **3.2** `src/voidx/ui/output/events/consumers.py`:
  - Replace L81-84, L99-102, L122-127: each calls `logging.getLogger(__name__).warning(...)` with an exception. Replace each with `log_internal_error(exc, context="ui_event_mirror_failed")` etc.

- [ ] **3.3** `src/voidx/ui/output/dock/nodes_clarify.py`:
  - Replace L53 `logger.debug("Clarify answer received for unknown clarify_id=%s", ...)` → `log_tool_event("ui_clarify_orphan", message=...)`

- [ ] **3.4** `src/voidx/ui/output/dock/nodes_checkpoint.py`:
  - Replace L55 `logger.debug("Checkpoint decision received for unknown checkpoint_id=%s", ...)` → `log_tool_event("ui_checkpoint_orphan", message=...)`

- [ ] **3.5** `src/voidx/ui/output/dock/nodes_status.py`:
  - Replace L59-60 `logging.getLogger("voidx.ui").debug("finish_status: unknown status_id=%s", ...)` → `log_tool_event("ui_status_orphan", ...)`

- [ ] **3.6** `src/voidx/ui/gateway/server.py`:
  - L61 `logger.warning(message)` is **redundant** — L62 calls `log_tool_event`. Delete L61.
  - L112 `logger.warning(message)` is **redundant** — L113 calls `log_tool_event`. Delete L112.
  - Replace L119 `logger.exception("Gateway websocket send failed...")` → `log_internal_error(exc, context="gateway_websocket_send")`

### Phase 4 — Tools

- [ ] **4.1** `src/voidx/tools/retry.py`:
  - The `logger` parameter is never passed by any caller (callers checked: fetch.py, goal_resolver.py, mcp/client/base.py). Delete the `logger` parameter and remove L37-41 `if logger: logger.warning(...)`. Also remove `import logging` if no other use.

- [ ] **4.2** `src/voidx/tools/search.py`:
  - Replace L276 `_logger.debug("Failed to read file during grep...")` → `log_tool_event("tool_grep_read_failed", ...)`. (Note: L277 already has `log_tool_event("grep_read_failed", ...)` — consolidate into one call.)
  - Replace L310 `_logger.debug("Failed to parse .gitignore")` → `log_tool_event("tool_gitignore_parse_failed", ...)`

### Phase 5 — LLM

- [ ] **5.1** `src/voidx/llm/catalog.py`:
  - Replace L199 `_logger.debug("Failed to fetch models for %s", ...)` → `log_tool_event("model_catalog_fetch_failed", ...)`
  - Replace L202 `_logger.debug("Unexpected error fetching models for %s", ...)` → `log_tool_event("model_catalog_fetch_error", ...)`

- [ ] **5.2** `src/voidx/llm/instruction.py`:
  - Replace L213-216 `logger.debug("Injected instruction file for %s: %s", ...)` → `log_llm_diagnostic("instruction_file_injected", filepath=..., candidate=...)`. Keep behind `self._debug` flag.

### Phase 6 — Cleanup deletions

- [ ] **6.1** `src/voidx/memory/transcript.py`: Delete L341-343 (2× log.debug)
- [ ] **6.2** `src/voidx/ui/output/browse.py`: Delete L62 and L70 (2× _logger.debug)
- [ ] **6.3** `src/voidx/selfupdate.py`: Delete L298 `logger.debug(...)`

### Phase 7 — Finalize

- [ ] **7.1** For any file that no longer uses its module-level logger, remove `import logging` and `log/logger/_logger = logging.getLogger(__name__)`.
- [ ] **7.2** Run `./test.py --backend` to confirm no regressions.

## Verification

| Task | Verification command |
|---|---|
| All phases | `./test.py --backend -v` — all tests green |
| Specific file changes | `python -c "from voidx.logging.tool_log import log_tool_event; print('OK')"` — import works |
| No dangling logging imports | `grep -rn "^import logging" src/voidx/ --include="*.py"` — review remaining ones are intentional (logging/*.py self-errors, and any new ones added by this plan) |

## Risks

1. **Event name collisions** — new events follow `{domain}_{event}` convention; grep for existing names before choosing each.
2. **Silent error loss** — some `log.debug` calls carry info that might be useful for future debugging. Mitigation: each deletion case was individually evaluated. If a debug call proves missed later, it's trivial to add back as `log_tool_event`.
3. **`tools/retry.py` contract change** — removing the `logger` parameter is technically a breaking signature change. But no caller passes it, and `retry_async` is internal API (no external consumers).
4. **`import logging` removal** — a file might use `logging` for something other than logging (e.g. exception types). Each removal is reviewed individually; `logging` module import stays if any other use exists.
