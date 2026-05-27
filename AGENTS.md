# voidx Project Instructions

## Build & Test
- Run tests: `.venv/Scripts/python.exe -m pytest tests/ -v`
- Run a single test: `.venv/Scripts/python.exe -m pytest tests/test_tools/test_basic.py -v`
- Check types: not yet configured (use Pydantic validation)

## Code Style
- No comments unless the WHY is non-obvious
- Pydantic for all configuration and tool inputs
- Tools: id in snake_case, precise descriptions
- Agent prompts: concise, rules-first, no fluff

## Architecture
- `agent/` — agent definitions, graph, state, prompts
- `tools/` — tool implementations (base, file_ops, search, bash, task, todo, webfetch, websearch)
- `llm/` — provider, context, instruction, compaction
- `memory/` — SQLite session store
- `ui/` — Rich terminal rendering
