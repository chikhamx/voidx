# voidx Source Agent Instructions

Python backend core for voidx.

## Module Map
- `voidx/agent/`: Agent runtime — layered as `application/` (services), `domain/` (policies/state/turn), `ports/` (interfaces), `infrastructure/` (LangGraph + adapters), `loop/` (controller/scheduler), `runtime/` (dispatcher/lifecycle), and `slash/` (slash command handlers).
- `voidx/agent/slash/`: Slash command handlers — runtime config, session, model, MCP/LSP, skills, profile, host, and IDE integration commands.
- `voidx/config/`: Settings & profiles — Pydantic models, MCP server config, API keys, permissions.
- `voidx/llm/`: Provider setup, prompt context, compaction, token usage.
- `voidx/mcp/`: MCP support — `client/` (JSON-RPC client), `manager.py` (lifecycle), `tool.py` (tool wrapper), `schema.py`, and `server/` (built-in MCP server implementations, e.g. voidx-web).
- `voidx/memory/`: SQLite-backed sessions, transcript, runtime snapshots, context frames.
- `voidx/permission/`: Permission engine — rules, sandbox, approval policy, wildcard matching.
- `voidx/runtime/`: Shared runtime — UI sink, task state, intent resolution.
- `voidx/skills/`: Skill system — registry, policy, bundled skills.
- `voidx/tools/`: Typed tool implementations and MCP/LSP adapters — `bash/` (shell execution, safety, routing), `file/` (read, write, edit, listing), plus `git/`, `shell/`, `powershell/`, `web/`, and top-level tools (search, todo, workflow, etc.).
- `voidx/lsp/`: Language Server Protocol client — manager, service, detector, schema.
- `voidx/workflow/`: Structured workflow runtime — DAG, nodes, policy, routing, reconciliation, schema.
- `voidx/logging/`: Request and tool logging.
- `voidx/ui/output/`: Output rendering — dock tree, streaming, capture, events, diff.
- `voidx/ui/protocol/`: UI protocol layer — v2 JSON-RPC models, schema, transcript, commands, requests.
- `voidx/ui/tools/`: UI-side tools — clipboard, file picker, skill picker, IDE integration.
- `voidx/ui/gateway/`: WebSocket gateway for web/desktop frontend.

## Testing
- `tests/`: pytest coverage — per-module test directories (agent, config, llm, lsp, mcp, memory, permission, runtime, skills, tools, ui, workflow, etc.) plus top-level install/npm packaging tests.
- Test layout mirrors `voidx/` module structure — one test directory per module.
- Fixtures and helpers live in `tests/conftest.py` and per-directory `conftest.py`.
- Full tests: `./test.py --backend` (covers `src/tests` + `tui/tests`)
- Focused tests: `./test.py --backend -- src/tests/test_tools/test_clarify_tool.py -v`

## Code Rules
- Use Pydantic models for config, tool inputs, and persisted structured data.
- Tool ids stay snake_case with precise, action-oriented descriptions.
- Prefer structured metadata over parsing rendered text.
- Keep prompts rules-first, concise, and specific to the agent role.
