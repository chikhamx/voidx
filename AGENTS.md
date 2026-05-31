# voidx Agent Instructions

## Project Shape
- `src/voidx/agent/`: LangGraph orchestration, roles, slash commands, runtime state.
- `src/voidx/tools/`: typed tool implementations and MCP/LSP adapters.
- `src/voidx/llm/`: provider setup, prompt context, compaction, token usage.
- `src/voidx/memory/`: SQLite-backed sessions, transcript, runtime snapshots.
- `src/voidx/ui/`: prompt_toolkit/Rich terminal UI and transcript rendering.
- `tests/`: pytest coverage for agent flow, permissions, tools, UI, MCP/LSP.

## Commands
- Full tests: `.venv/bin/python -m pytest tests/ -v`
- Focused tests: `.venv/bin/python -m pytest tests/test_tools/test_basic.py -v`
- Build wheel: `.venv/bin/python scripts/package.py`

## Code Rules
- Keep modules small and named by responsibility; avoid `*_parts` directories.
- Use Pydantic models for config, tool inputs, and persisted structured data.
- Tool ids stay snake_case with precise, action-oriented descriptions.
- Prefer structured metadata over parsing rendered text.
- Keep prompts rules-first, concise, and specific to the agent role.
- Do not add comments unless they explain non-obvious intent or constraints.

## Safety
- Do not commit `voidx.json`, `.voidx/`, `.env*`, or local credentials.
- Preserve user work in a dirty tree; never revert unrelated changes.
- Run the relevant focused tests before broad test runs.
