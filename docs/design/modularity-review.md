# Codebase Modularity & File Size Review

> **Status: Active** — Review findings, awaiting prioritization and implementation scheduling.
> **Date: 2026-06-06**

## Scope

Review of the `src/voidx/` codebase (139 Python files, ~26K lines) focusing on:

1. Single-file size — are any files too large or mixing concerns?
2. Modularity — are responsibilities well-separated?
3. Cross-module coupling — are there problematic dependency directions?

## Verdict: NEEDS_CHANGE

The codebase is well-structured at the top level with clear package boundaries. The mixin-based decomposition of `VoidXGraph` and the dock subpackage split are good patterns already in use. However, there are 10 files over 500 lines that mix concerns, a significant coupling issue between `agent→ui`, and reverse-dependency violations that should be addressed.

---

## 1. Files Over 500 Lines

### 1.1 `config.py` — 737 lines, 12 classes + 5 enums

**Problem:** Holds enums, model configs, MCP configs, LSP configs, permission presets, settings I/O, and CLI argument parsing in a single file. Imported by nearly every module.

**Recommendation:** Split into a `config/` package:

```
config/
  __init__.py    — re-exports for backward compatibility
  enums.py       — SandboxMode, ApprovalPolicy, ApprovalReviewer, CodeIde, PermissionMode
  models.py      — Profile, ModelConfig, McpServerConfig, LspServerConfig
  settings.py    — Settings class, load/save logic
  cli.py         — CLI argument parsing
  permissions.py — permission_mode_defaults, permission_mode_reviewer_default
```

### 1.2 `mcp/client.py` — 723 lines

**Problem:** Mixes 3 transport implementations (stdio, SSE, streamable-http) in one class. Each transport has its own start/stop/read/write logic, making the class hard to navigate and extend.

**Recommendation:** Split into `mcp/client/` with a base class + transport strategy:

```
mcp/client/
  __init__.py          — re-exports McpClient
  base.py              — McpClient base (request tracking, JSON-RPC, lifecycle)
  stdio_transport.py   — StdioTransport mixin
  sse_transport.py     — SseTransport mixin
  http_transport.py    — StreamableHttpTransport mixin
```

### 1.3 `agent/graph/run_loop.py` — 673 lines

**Problem:** `GraphRunLoopMixin` handles startup display, user input, session management, transcript restore, and the main run loop. These are distinct responsibilities.

**Recommendation:** Extract into focused mixins:

```
agent/graph/
  run_loop.py        — main run loop + input handling (core)
  session_mixin.py   — session resume, _resume_session, _list_sessions
  transcript_mixin.py — _restore_transcript_snapshot, transcript save/restore
```

### 1.4 `ui/tui/app.py` — 628 lines

**Problem:** `PureTui.__init__` is ~100 lines of state initialization. The class handles input, choices, text prompts, command palette, file attachments, and submit flow. Already partially decomposed via mixins, but the remaining body is still too large.

**Recommendation:** Extract choice and text-prompt logic:

```
ui/tui/
  app.py               — PureTui core (init, submit, main loop)
  choice_mixin.py      — _ChoicePromptMixin (choice queue, selection, overlay)
  text_prompt_mixin.py — _TextPromptMixin (text queue, secret input, save/restore)
```

### 1.5 `ui/tui/renderer.py` — 592 lines

**Problem:** `_TerminalRendererMixin` handles frame rendering, status bar, choice overlay, attachment panel, and command palette rendering.

**Recommendation:** Split overlay/panel rendering:

```
ui/tui/
  renderer.py   — core frame rendering, cursor positioning, status bar
  overlays.py   — choice overlay, attachment panel, command palette rendering
```

### 1.6 `permission/engine.py` — 567 lines

**Problem:** Mixes capability classification, `BASIC_RULES` definition, `PermissionContext`, `PermissionEngine`, and mode-specific overlay logic.

**Recommendation:**

```
permission/
  engine.py    — PermissionEngine (decision logic only)
  rules.py     — BASIC_RULES, PermissionCapability, capability classification
  context.py   — PermissionContext dataclass
```

### 1.7 `ui/output/dock/nodes.py` — 545 lines

**Problem:** `DockNodeMixin` has ~20 `append_*` / `update_*` methods covering startup, status, streaming, tool calls, and errors.

**Recommendation:** Group by responsibility:

```
ui/output/dock/
  nodes.py           — DockNodeMixin base (append_message, append_error)
  nodes_startup.py   — append_startup, update startup
  nodes_status.py    — append_status, update_status, status tick
  nodes_streaming.py — append_stream, update_stream, append_tool_call, update_tool_result
```

### 1.8 `ui/output/dock/app.py` — 531 lines

**Problem:** `BottomInputDock` mixes lifecycle, rendering, status management, and input handling.

**Recommendation:** Extract status management:

```
ui/output/dock/
  app.py    — BottomInputDock core (lifecycle, rendering, input)
  status.py — DockStatusRecord, status_records dict, status tick logic
```

### 1.9 `agent/graph/core.py` — 520 lines

**Problem:** `VoidXGraph.__init__` is large and the class still has `_build_graph`, `_invoke_model`, `_should_continue`, `_route_after_tool` — graph topology mixed with invocation logic.

**Recommendation:** Extract graph topology:

```
agent/graph/
  core.py       — VoidXGraph class (init, _invoke_model, _should_continue)
  topology.py   — _build_graph, node definitions, edge routing
```

### 1.10 `lsp/detector.py` — 512 lines

**Problem:** ~200 lines are static data tables (`LANGUAGE_DEFAULTS`, `_EXTENSION_MAP`, `_NPM_PACKAGE_MAP`, platform-specific paths). Actual detection logic is interleaved with data.

**Recommendation:**

```
lsp/
  detector.py       — detection logic only
  detector_data.py  — LANGUAGE_DEFAULTS, _EXTENSION_MAP, _NPM_PACKAGE_MAP, platform paths
```

---

## 2. Cross-Module Coupling

### 2.1 [HIGH] agent → ui (28 imports)

The agent layer imports from `ui` 28 times, making it impossible to run the agent headlessly (e.g., in CI or as a library) without pulling in Rich, prompt_toolkit, and the entire TUI stack.

**Breakdown of agent→ui imports:**

| ui submodule | Import count | Key consumers |
|---|---|---|
| `ui.output.console` | 6 | core.py, streaming.py |
| `ui.output.events` | 5 | core.py, run_loop.py |
| `ui.output.dock` | 4 | core.py, run_loop.py |
| `ui.session` | 3 | run_loop.py |
| `ui.output.tree` | 3 | core.py |
| `ui.transcript` | 1 | run_loop.py |
| `ui.gateway` | 2 | run_loop.py |
| `ui.commands` | 1 | run_loop.py |
| `ui.protocol` | 1 | run_loop.py |
| `ui.tools.code_ide` | 1 | slash/handler.py |
| `ui.output.capture` | 1 | streaming.py |

**Recommendation:** Introduce an abstract output/sink interface in `agent/`:

```python
# agent/output_sink.py
class AgentOutputSink(Protocol):
    async def show_startup(self, model: str, provider: str, workspace: str, ...) -> None: ...
    async def append_message(self, text: str, style: str = "") -> None: ...
    async def update_status(self, status_id: str, label: str, ...) -> None: ...
    async def request_input(self, prompt: str) -> str: ...
    # ... etc
```

The UI layer implements this protocol. The agent receives the sink via dependency injection (constructor or runtime context) and never imports from `ui/` directly.

### 2.2 [MEDIUM] tools → agent (7 imports)

Tools import from `agent.task_state` and `agent.runtime_context`, creating a circular dependency direction (agent uses tools, tools import from agent).

**Affected files:**
- `tools/on_intent.py` → `agent.task_state`, `agent.runtime_context`
- `tools/clarify.py` → `agent.task_state`, `agent.runtime_context`
- `tools/plan_checkpoint.py` → `agent.task_state`, `agent.runtime_context`
- `tools/agent.py` → `agent.agents`

**Recommendation:** Move shared types (`TaskIntent`, `ToolStatePatch`, `InteractionMode`) into a `voidx.shared_types` or `voidx.agent.contracts` module that both `tools` and `agent` can import from without creating circular coupling. Alternatively, pass these as parameters rather than importing the types.

### 2.3 [MEDIUM] memory → agent (2 imports)

`memory/runtime_state.py` imports `InteractionMode`, `TaskIntent`, `PendingApproval`, `TaskPhase`, `TaskRun`, `TaskRunStatus`, `TaskState` from `agent.runtime_context` and `agent.task_state`. The memory layer should not depend on agent-specific types.

**Recommendation:** Define persistence-oriented dataclasses in `memory/` that are agnostic of agent internals. Map between agent types and memory types at the boundary (in `agent/` or a thin adapter layer).

### 2.4 [LOW] SlashHandler tightly coupled to VoidXGraph internals

`SlashHandler` holds a direct reference to `VoidXGraph` (`self._g`) and accesses private methods like `_compact_session_history`, `_persist_runtime_state`, `_permission`, `_session`. This makes refactoring `VoidXGraph` fragile.

**Recommendation:** Define a protocol that exposes only what slash commands need:

```python
class SlashCommandHost(Protocol):
    config: Config
    _session: SessionInfo | None
    _permission: PermissionService
    async def _compact_session_history(self, force: bool) -> bool: ...
    async def _persist_runtime_state(self) -> None: ...
    # ... only what slash commands actually use
```

---

## 3. Full Cross-Module Import Matrix

```
agent → llm:        14
agent → memory:      9
agent → permission:  3
agent → tools:      12
agent → ui:         28   ⚠️ HIGH
agent → lsp:        1
memory → agent:      2   ⚠️ reverse dependency
permission → ui:     1
tools → agent:       7   ⚠️ reverse dependency
tools → permission:  2
tools → lsp:         2
ui → agent:          1
ui → llm:            2
ui → memory:         1
ui → tools:          1
mcp → permission:    1
mcp → tools:         2
lsp → tools:         1
```

**Ideal dependency direction:** config → llm → memory → tools → permission → agent → ui

**Current violations:** agent→ui (wrong direction), tools→agent (circular), memory→agent (circular)

---

## 4. What's Working Well

- **Mixin decomposition of VoidXGraph** — `GraphRunLoopMixin`, `GraphCompactionMixin`, `GraphToolExecutionMixin`, `GraphPermissionMixin` is a clean pattern that keeps `core.py` focused on graph topology.
- **Slash command mixins** — `SlashLspMixin`, `SlashMcpMixin`, `SlashModelMixin`, etc. keep `handler.py` as a thin dispatcher.
- **Dock subpackage** — Already split into `app.py`, `nodes.py`, `formatting.py`, `state.py`. Good pattern, just needs further decomposition of the two large files.
- **Clean bottom-up dependencies** — `config` has zero inbound cross-module imports; `llm` and `memory` have minimal outbound coupling.
- **Protocol-based contracts** — `GraphComponentHost` and friends in `agent/graph/contracts.py` are a good pattern that should be expanded to the agent→ui boundary.

---

## 5. Suggested Implementation Priority

| Priority | Item | Impact | Effort |
|---|---|---|---|
| P0 | agent→ui decoupling (AgentOutputSink protocol) | Enables headless/CI usage | Medium |
| P1 | Split `config.py` into package | Reduces god-module risk | Low |
| P1 | Split `mcp/client.py` by transport | Improves extensibility | Medium |
| P2 | Split `agent/graph/run_loop.py` | Improves readability | Low |
| P2 | Split `permission/engine.py` | Improves readability | Low |
| P2 | Extract shared types for tools↔agent | Breaks circular dependency | Low |
| P3 | Split remaining 500+ line files | Code hygiene | Low each |
| P3 | memory→agent decoupling | Clean layering | Medium |
| P3 | SlashHandler protocol extraction | Refactoring safety | Low |
