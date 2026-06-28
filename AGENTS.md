# voidx Agent Instructions

## Project Shape
- `src/voidx/agent/graph/`: LangGraph orchestration — turn loop, compaction, subagents, tool execution, permissions.
- `src/voidx/agent/slash/`: Slash command handlers — runtime config, session, model, MCP/LSP, skills, profile, host, and IDE integration commands.
- `src/voidx/config/`: Settings & profiles — Pydantic models, MCP server config, API keys, permissions.
- `src/voidx/llm/`: Provider setup, prompt context, compaction, token usage.
- `src/voidx/mcp/`: MCP client manager, tool wrapper, schema.
- `src/voidx/mcp_servers/`: Built-in MCP server implementations (e.g. voidx-web).
- `src/voidx/memory/`: SQLite-backed sessions, transcript, runtime snapshots, context frames.
- `src/voidx/permission/`: Permission engine — rules, sandbox, approval policy, wildcard matching.
- `src/voidx/runtime/`: Shared runtime — UI sink, task state, intent resolution.
- `src/voidx/skills/`: Skill system — registry, policy, bundled skills.
- `src/voidx/tools/`: Typed tool implementations and MCP/LSP adapters — `bash/` (shell execution, safety, routing) and `file_ops/` (read, write, edit, file listing).
- `src/voidx/lsp/`: Language Server Protocol client — manager, service, detector, schema.
- `src/voidx/workflow/`: Structured workflow runtime — DAG, nodes, policy, routing, reconciliation, schema.
- `src/voidx/logging/`: Request and tool logging.
- `src/voidx/ui/tui/`: Pure terminal TUI — input parser, editor, panels, renderer, state.
- `src/voidx/ui/output/`: Output rendering — dock tree, streaming, capture, events, diff.
- `src/voidx/ui/protocol/`: UI protocol layer — envelope, schema, transcript, commands, requests.
- `src/voidx/ui/tools/`: UI-side tools — clipboard, file picker, skill picker, IDE integration.
- `src/voidx/ui/gateway/`: WebSocket gateway for web/desktop frontend.
- `tests/`: pytest coverage — per-module test directories (agent, config, llm, lsp, mcp, memory, permission, runtime, skills, tools, ui, workflow, etc.) plus top-level install/npm packaging tests.
- `frontend/test/`: vitest coverage for frontend JS modules — `slash`, `render`, `markdown`, `stream`, `main`; `setup.js` injects the DOM skeleton.

## Runtime Environment
- Use `./python.sh` (Unix) or `.\python.ps1` (Windows) as the Python entry point — these locate the voidx venv under `VOIDX_HOME` and forward all arguments. See `docs/dev-guide.md` for details. Commands below use the Unix form; Windows users substitute `.\python.ps1`.

## Commands
- Full tests: `./python.sh -m pytest tests/ -v`
- Focused tests: `./python.sh -m pytest tests/test_tools/test_basic.py -v`
- Build wheel: `./python.sh scripts/package.py`
- Web UI gateway: `./python.sh -m voidx.main --web` (open frontend with `?ws=<gateway-url>`)
- Headless web backend: `./python.sh -m voidx.main --web --web-headless`
- Export UI protocol schema: `./python.sh scripts/export_ui_protocol_schema.py`
- Frontend dev server: `cd frontend && npm run dev`
- Desktop dev shell: `cd desktop && npm run dev` (spawns Python sidecar via Tauri)
- Frontend tests (all): `cd frontend && npm test`
- Frontend tests (focused): `cd frontend && npx vitest run test/render.test.js`

## Code Rules
- Keep modules small and named by responsibility.
- Use Pydantic models for config, tool inputs, and persisted structured data.
- Tool ids stay snake_case with precise, action-oriented descriptions.
- Prefer structured metadata over parsing rendered text.
- Keep prompts rules-first, concise, and specific to the agent role.
- Do not add comments unless they explain non-obvious intent or constraints.
- Frontend modules: export private functions that tests need to reach; guard module-top-level side effects (e.g. `bootstrap()`) with `import.meta.env.TEST` so importing under vitest stays pure.

## Document Lifecycle
- Design docs live in `docs/specs/` while in progress.
- When implementation is **fully complete** (code + tests exist, not just stubs or string references), move the doc to `docs/archive/` and add a `> **Status: Done**` header.
- Do **not** archive based on keyword search alone — verify the actual implementation files exist and are functional.
- `docs/design/` is for exploratory/RFC-stage docs; `docs/specs/` is for approved designs awaiting or in implementation.

## Releasing
- Release flow and version file checklist: `docs/releasing.md` (single source of truth — do not duplicate).

## Frontend Testing
- Framework: vitest + jsdom (configured in `frontend/vite.config.js` under `test`).
- `frontend/test/setup.js` runs at module top level to inject the DOM skeleton (`#transcript`, `#todo-panel`, `#composer`, etc.) before any source module imports; `beforeEach` resets dynamic content between tests.
- Test files live in `frontend/test/`, named `<module>.test.js`, mirroring `frontend/src/<module>.js`.
- Globals are enabled (`globals: true`) — `describe`/`it`/`expect` are available without import.
- Stateful modules expose a `_resetForTest()` export to clear module-level state in `beforeEach`.
- When adding a new frontend module: create `frontend/test/<module>.test.js`, export any private functions the tests need, and guard top-level side effects with `import.meta.env.TEST`.

## Safety
- Do not commit `.voidx/`, `.env*`, or local credentials.
- Preserve user work in a dirty tree; never revert unrelated changes.
- Run the relevant focused tests before broad test runs.
