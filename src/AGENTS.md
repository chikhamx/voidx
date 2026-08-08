# voidx Source Agent Instructions

Python backend core for voidx.

## Module Map
- `voidx/agent/`: Agent feature — `domain/` owns state and policy, `ports/` owns interfaces, `application/` owns use cases and runtime orchestration, `adapters/` owns LangGraph, persistence, tools, and subagent integrations.
- `voidx/tooling/`: Tool platform — `domain/`, `ports/`, `application/`, `policy/`, `adapters/`, and `builtin/` implementations.
- `voidx/presentation/`: Terminal/web presentation — output, protocol, gateway, slash commands, presentation adapters, and UI-side tools.
- `voidx/config/`: Settings, profiles, config models, and persistence adapters.
- `voidx/llm/`: Provider/model domain, catalog application services, concrete model adapters, compaction, and token usage.
- `voidx/mcp/` and `voidx/lsp/`: Layered domain/ports/application/adapters for external protocol clients.
- `voidx/skills/`: Skill domain and application services.
- `voidx/persistence/`: Shared SQLite/JSONL infrastructure and migrations.
- `voidx/platform/`: OS paths, processes, retry, execution context, and file-type helpers.
- `voidx/observability/`: Request, tool, external, and internal-error logging.
- `voidx/update/`: Version check and explicit self-update service.
- `voidx/bootstrap/`: The only composition root; wires concrete adapters and owns CLI startup.
- `voidx/main.py`: Thin package entrypoint that exports `voidx.bootstrap.cli`.

## Dependency Direction
- Domain and ports do not import concrete adapters or presentation.
- Application code depends on its feature domain/ports plus approved foundation packages.
- Cross-feature concrete adapter composition belongs only in `voidx/bootstrap/`.
- Agent core does not import presentation; presentation capabilities are injected by bootstrap.
- `voidx/main.py` imports only `voidx.bootstrap`.

## Testing
- Tests mirror final ownership under `src/tests/`, for example `test_agent/adapters/langgraph`, `test_tooling`, `test_presentation`, `test_observability`, `test_platform`, and `test_update`.
- Full backend: `./test.py --backend`.
- Focused example: `./test.py --backend -- src/tests/test_agent/adapters/langgraph -v`.
- Architecture and contracts: `./test.py --backend -- src/tests/test_architecture src/tests/test_contracts -v`.

## Code Rules
- Use Pydantic models for config, tool inputs, and persisted structured data.
- Tool ids stay snake_case with precise, action-oriented descriptions.
- Prefer structured metadata over parsing rendered text.
- Keep prompt rules concise and specific to the agent role.
