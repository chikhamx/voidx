# voidx Agent Instructions

## Project Shape
- `src/voidx/agent/graph/`: LangGraph orchestration — turn loop, compaction, subagents, tool execution, permissions.
- `src/voidx/agent/slash/`: Slash command handlers — /mcp, /model, /lsp, /session, /skills, /init.
- `src/voidx/config/`: Settings & profiles — Pydantic models, MCP server config, API keys, permissions.
- `src/voidx/llm/`: Provider setup, prompt context, compaction, token usage.
- `src/voidx/mcp/`: MCP client manager, tool wrapper, schema.
- `src/voidx/mcp_servers/`: Built-in MCP server implementations (e.g. voidx-web).
- `src/voidx/memory/`: SQLite-backed sessions, transcript, runtime snapshots, context frames.
- `src/voidx/permission/`: Permission engine — rules, sandbox, approval policy, wildcard matching.
- `src/voidx/runtime/`: Shared runtime — UI sink, task state, intent resolution.
- `src/voidx/skills/`: Skill system — registry, policy, bundled skills.
- `src/voidx/tools/`: Typed tool implementations and MCP/LSP adapters.
- `src/voidx/ui/tui/`: Pure terminal TUI — input parser, editor, panels, renderer, state.
- `src/voidx/ui/output/`: Output rendering — dock tree, streaming, capture, events, diff.
- `src/voidx/ui/gateway/`: WebSocket gateway for web/desktop frontend.
- `tests/`: pytest coverage — test_agent/, test_tools/, plus top-level UI and integration tests.

## Commands
- Full tests: `.venv/bin/python -m pytest tests/ -v`
- Focused tests: `.venv/bin/python -m pytest tests/test_tools/test_basic.py -v`
- Build wheel: `.venv/bin/python scripts/package.py`
- Web UI gateway: `.venv/bin/python -m voidx.main --web` (open frontend with `?ws=<gateway-url>`)
- Headless web backend: `.venv/bin/python -m voidx.main --web --web-headless`
- Export UI protocol schema: `.venv/bin/python scripts/export_ui_protocol_schema.py`
- Frontend dev server: `cd frontend && npm run dev`
- Desktop dev shell: `cd desktop && npm run dev` (spawns Python sidecar via Tauri)

## Code Rules
- Keep modules small and named by responsibility; avoid `*_parts` directories.
- Use Pydantic models for config, tool inputs, and persisted structured data.
- Tool ids stay snake_case with precise, action-oriented descriptions.
- Prefer structured metadata over parsing rendered text.
- Keep prompts rules-first, concise, and specific to the agent role.
- Do not add comments unless they explain non-obvious intent or constraints.

## Document Lifecycle
- Design docs live in `docs/specs/` while in progress.
- When implementation is **fully complete** (code + tests exist, not just stubs or string references), move the doc to `docs/archive/` and add a `> **Status: Done**` header.
- Do **not** archive based on keyword search alone — verify the actual implementation files exist and are functional.
- `docs/design/` is for exploratory/RFC-stage docs; `docs/specs/` is for approved designs awaiting or in implementation.

## Safety
- Do not commit `.voidx/`, `.env*`, or local credentials.
- Preserve user work in a dirty tree; never revert unrelated changes.
- Run the relevant focused tests before broad test runs.
