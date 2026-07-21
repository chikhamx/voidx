# Chat Runtime Implementation Spec

## Goal

Provide an isolated Chat turn path that reuses `AgentRuntime.run_turn()` while keeping coding tools, permissions, sessions, and runtime state behavior unchanged.

## Implemented boundaries

- `src/voidx/agent/domain/chat_policy.py`
  - No workspace: `websearch`, `webfetch`, and the existing `mcp` gateway.
  - Workspace bound: additionally `read`, `glob`, `grep`, and `lsp`.
  - Shell, write, edit, delete, move, git, agent, and subagent tools are denied.
  - Denials never request approval; local paths must remain under the normalized workspace.
- `src/voidx/agent/application/chat_service.py`
  - Creates `chat:<session_id>` threads and `runtime_profile="chat"` sessions.
  - Defaults to no workspace and delegates the turn exclusively to `AgentRuntime.run_turn()`.
- `src/voidx/memory/session.py` and `src/voidx/memory/store.py`
  - Add `runtime_profile` with a coding default.
  - Schema v1 to v2 migration preserves existing sessions as coding sessions.
- `src/voidx/agent/infrastructure/langgraph/runtime/turn_runner.py`
  - Carries the fixed Chat tool view for the current turn only.
- `src/voidx/agent/infrastructure/langgraph/execution.py`
  - Filters LLM-visible tools and bypasses coding permission authorization for Chat turns.
- `src/voidx/agent/infrastructure/langgraph/runtime/tool_executor/executor.py`
  - Rechecks the fixed view immediately before execution and returns structured `tool_denied` results.

## Invariants

- Coding requests without a Chat context use the existing registry, prompt, permission service, and LangGraph topology.
- Chat cannot expand its tool view through a permission response.
- Chat sessions remain in the global memory store; workspace controls only local resource scope.
- Chat application code uses `voidx.memory.service`, not internal memory modules.

## Verification

```bash
./test.py --backend -- src/tests/test_agent/test_chat_policy.py src/tests/test_agent/test_chat_service.py src/tests/test_memory/test_chat_session.py -v
./test.py --backend -- src/tests/test_agent
./test.py --backend
python3 -m compileall -q src/voidx/agent src/voidx/memory
git diff --check
```

## Follow-up

Gateway/frontend RPC methods and Chat UI state should be added as a separate change after the backend protocol shape is approved; existing coding UI/API behavior must not be changed as part of that work.
