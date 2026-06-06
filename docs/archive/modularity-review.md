# Codebase Modularity & File Size Review

> **Status: Done** — Approved modularity remediation implemented and verified; remaining items are follow-up hardening, not open findings from the original review.
> **Date: 2026-06-06**

## Scope

Review of the `src/voidx/` codebase (154 Python files, ~26.9K lines) focusing on:

1. Single-file size — are any files too large or mixing concerns?
2. Modularity — are responsibilities well-separated?
3. Cross-module coupling — are there problematic dependency directions?

## Verdict: REMEDIATED

The original findings have been addressed: the listed 500+ line files have been split below the threshold, reverse dependencies from tools/memory/permission into agent/ui are gone, and `src/voidx/agent` no longer statically imports `voidx.ui`. Importing `voidx.agent.graph` does not load any `voidx.ui` modules. The remaining architectural hardening is to replace the transitional lazy UI adapter with explicit sink/session injection for a fully standalone headless/library runtime.

---

## 0. Remediation Completed

Completed on 2026-06-06:

- Added `voidx.diffing` for pure diff parsing, generation, stats, and git diff helpers. `tools` now use this module instead of importing `ui.output.diff`; `ui.output.diff` remains the rendering/compatibility entry point.
- Added `voidx.runtime` for shared runtime contracts: `InteractionMode`, `TaskIntent`, `PendingApproval`, `TaskState`, `TaskRun`, `ToolStatePatch`, and `resolve_turn_intent`.
- Added `voidx.runtime.ui.AgentUiSink` plus a lazy UI adapter. Agent modules now import the neutral boundary instead of static `voidx.ui` modules.
- Kept `voidx.agent.runtime_context` and `voidx.agent.task_state` as compatibility import paths for existing callers.
- Moved `tools/on_intent.py`, `tools/clarify.py`, `tools/plan_checkpoint.py`, and `memory/runtime_state.py` to `voidx.runtime` imports.
- Changed `PermissionService` to accept an injected notifier, removing its direct dependency on `ui.output.console`.
- Changed `AgentTool` to receive the child-agent resolver/catalog by dependency injection, removing its direct dependency on `agent.agents`.
- Split `config.py` into `config/` modules with a compatibility re-export in `config/__init__.py`.
- Split `mcp/client.py` into `mcp/client/` transport modules.
- Split `agent/graph/run_loop.py` into run loop, turn, session, and transcript mixins.
- Split `agent/graph/core.py` by moving topology and wiring helpers into `topology.py` and `wiring.py`.
- Split TUI choice/text/clipboard/terminal behavior and renderer overlays into focused modules.
- Split permission rules/context from permission engine.
- Split dock app status handling and node rendering groups into focused modules.
- Split LSP detector static data into `detector_data.py`.
- Added `SlashCommandHost` and extracted session slash commands into `slash/session.py`.

Current targeted scan result:

```
tools → ui:        0
permission → ui:   0
memory → ui:       0
tools → agent:     0
memory → agent:    0
agent → ui:        0
agent import loads ui modules: false
files >= 500 lines: 0
```

---

## 1. Files Over 500 Lines

All original 500+ line files are now under 500 lines, and no Python file in `src/voidx/` is currently at or above 500 lines.

Current largest files:

| File | Lines |
|---|---:|
| `ui/output/tree.py` | 488 |
| `ui/tui/renderer.py` | 486 |
| `ui/tui/app.py` | 479 |
| `llm/compaction.py` | 477 |
| `agent/agents.py` | 443 |
| `agent/slash/model.py` | 441 |
| `agent/graph/core.py` | 430 |
| `agent/slash/handler.py` | 428 |

### 1.1 `config.py` — DONE

**Problem:** Holds enums, model configs, MCP configs, LSP configs, permission presets, settings I/O, and CLI argument parsing in a single file. Imported by many modules.

**Implemented:** Split into a `config/` package:

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

**Implemented:** Split into `mcp/client/` with a base class + transport strategy:

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

**Implemented:** Extracted into focused mixins:

```
agent/graph/
  run_loop.py         — main run loop + input handling (core)
  turn_mixin.py      — single-turn execution
  session_mixin.py   — runtime state persistence
  transcript_mixin.py — transcript save/restore
```

### 1.4 `ui/tui/app.py` — DONE

**Problem:** `PureTui.__init__` is ~100 lines of state initialization. The class handles input, choices, text prompts, command palette, file attachments, and submit flow. Already partially decomposed via mixins, but the remaining body is still too large.

**Implemented:** Extracted choice, text-prompt, clipboard, and terminal logic:

```
ui/tui/
  app.py               — PureTui core (init, submit, main loop)
  choice_mixin.py      — _ChoicePromptMixin (choice queue, selection, overlay)
  text_prompt_mixin.py — _TextPromptMixin (text queue, secret input, save/restore)
  clipboard_mixin.py   — clipboard image attach flow
  terminal_mixin.py    — terminal paste/input helpers
```

### 1.5 `ui/tui/renderer.py` — DONE

**Problem:** `_TerminalRendererMixin` handles frame rendering, status bar, choice overlay, attachment panel, and command palette rendering.

**Implemented:** Split overlay/panel rendering:

```
ui/tui/
  renderer.py — core frame rendering, cursor positioning, status bar
  overlays.py — choice overlay, attachment panel, command palette rendering
```

### 1.6 `permission/engine.py` — DONE

**Problem:** Mixes capability classification, `BASIC_RULES` definition, `PermissionContext`, `PermissionDecision`, and mode-specific overlay logic.

**Implemented:**

```
permission/
  engine.py  — authorization flow and decision logic
  rules.py   — BASIC_RULES, PermissionCapability, capability classification
  context.py — PermissionContext and PermissionDecision dataclasses
```

### 1.7 `ui/output/dock/app.py` — DONE

**Problem:** `BottomInputDock` mixes lifecycle, rendering, status management, and input handling.

**Implemented:** Extracted status management:

```
ui/output/dock/
  app.py    — BottomInputDock core (lifecycle, rendering, input)
  status.py — DockStatusRecord, status_records dict, status tick logic
```

### 1.8 `ui/output/dock/nodes.py` — DONE

**Problem:** `DockNodeMixin` has ~20 `append_*` / `update_*` methods covering startup, status, streaming, tool calls, file changes, and errors.

**Implemented:** Grouped by responsibility:

```
ui/output/dock/
  nodes.py           — DockNodeMixin base (append_message, append_error)
  nodes_startup.py   — append_startup, update startup
  nodes_status.py    — append_status, update_status, status tick
  nodes_permission.py — permission prompt rendering
  stream.py          — stream rendering
```

### 1.9 `agent/graph/core.py` — DONE

**Problem:** `VoidXGraph.__init__` is large and the class still mixes graph topology, model invocation, routing, and dependency wiring.

**Implemented:** Extracted graph topology and wiring helpers:

```
agent/graph/
  core.py     — VoidXGraph class and invocation flow
  topology.py — _build, node definitions, edge routing
  wiring.py   — tool/permission/MCP/LSP dependency construction
```

### 1.10 `lsp/detector.py` — DONE

**Problem:** ~200 lines are static data tables (`LANGUAGE_DEFAULTS`, `_EXTENSION_MAP`, `_NPM_PACKAGE_MAP`, platform-specific paths). Actual detection logic is interleaved with data.

**Implemented:**

```
lsp/
  detector.py      — detection logic only
  detector_data.py — LANGUAGE_DEFAULTS, _EXTENSION_MAP, _NPM_PACKAGE_MAP, platform paths
```

---

## 2. Cross-Module Coupling

### 2.1 [DONE] agent → ui static imports removed

The agent layer no longer statically imports UI modules. Graph, slash, streaming, permission, transcript, and subagent code now import through `voidx.runtime.ui`, which exposes `AgentUiSink` and lazy adapter functions/types. A targeted import check also confirms that importing `voidx.agent.graph` does not load `voidx.ui`.

Implemented boundary:

```python
class AgentUiSink(Protocol):
    def set_debug(self, value: bool) -> None: ...
    def print(...) -> None: ...
    def warn(self, message: str) -> None: ...
    def error(self, message: str) -> None: ...
    def step_header(self, n: int, max_n: int, agent: str = "") -> None: ...
    def tool_call(self, tool_name: str, args: dict[str, object]) -> None: ...
    def tool_done(self, tool_name: str, elapsed: float, ok: bool = True) -> None: ...
    def tool_result(self, text: str) -> None: ...
    def diff(self, diff_text: str) -> None: ...
```

**Remaining hardening:** `voidx.runtime.ui` is currently a transitional lazy adapter that imports concrete UI modules only when interactive behavior is used. For a fully standalone headless/library API, move this from a global lazy adapter to explicit sink/session injection from `main.py` or the UI entrypoint.

### 2.2 [DONE] tools/memory/permission reverse dependencies

The original review found:

- `tools → agent` via `ToolStatePatch`, `TaskIntent`, and child-agent definitions.
- `memory → agent` via persisted runtime state models.
- `permission → ui` via `VoidConsole`.
- `tools → ui` via diff helpers.

These are now resolved by `voidx.runtime`, `voidx.diffing`, notifier injection, and `AgentTool` catalog injection.

### 2.3 [DONE] SlashHandler host protocol introduced

`SlashHandler` now depends on `SlashCommandHost`, a protocol exposing the graph surface needed by slash commands. Session lifecycle commands were also extracted into `slash/session.py`.

Implemented shape:

```python
class SlashCommandHost(Protocol):
    config: Any
    _session: Any | None
    _permission: Any
    async def _compact_session_history(self, *, force: bool = True) -> bool: ...
    async def _persist_runtime_state(self) -> None: ...
```

---

## 3. Dependency Direction

Use this direction as the target:

```
config/runtime/diffing
  ↓
llm / memory / permission / tools / lsp / mcp
  ↓
agent
  ↓
ui
```

Allowed examples:

- `tools → runtime`
- `memory → runtime`
- `ui → agent`
- `agent → tools`

Disallowed examples:

- `tools → agent`
- `memory → agent`
- `tools/permission/memory → ui`
- `agent → ui` once the P0 sink boundary exists

Current targeted violations:

```
agent → ui:        0
tools → ui:        0
permission → ui:   0
memory → ui:       0
tools → agent:     0
memory → agent:    0
```

The remaining caveat is `runtime.ui -> ui` lazy adapter coupling, which is intentional for this remediation slice and should be replaced with explicit dependency injection in a later hardening pass.

---

## 4. What's Working Well

- **Mixin decomposition of VoidXGraph** — `GraphRunLoopMixin`, `GraphCompactionMixin`, `GraphToolExecutionMixin`, `GraphPermissionMixin` is a good pattern for splitting behavior.
- **Slash command mixins** — `SlashLspMixin`, `SlashMcpMixin`, `SlashModelMixin`, etc. keep `handler.py` as a thin dispatcher.
- **Dock subpackage** — Already split into `app.py`, `nodes.py`, `formatting.py`, `state.py`.
- **Protocol-based contracts** — `GraphComponentHost` and friends in `agent/graph/contracts.py` are a good pattern to expand to the UI boundary.
- **Neutral shared modules now exist** — `voidx.runtime` and `voidx.diffing` provide a clean home for contracts used by multiple layers.
- **Lazy UI boundary** — `agent` can now import without importing concrete UI modules.

---

## 5. Suggested Implementation Priority

| Priority | Item | Impact | Effort | Status |
|---|---|---|---|---|
| P0 | Remove direct `agent → ui` imports | Completes boundary inversion | High | Done |
| P0 | Agent UI sink/session boundary | Enables headless/library agent usage | High | Partial: lazy boundary done; explicit injection remains |
| P1 | Split `mcp/client.py` by transport | Improves extensibility | Medium | Done |
| P1 | Split `agent/graph/run_loop.py` | Reduces graph runtime complexity | Medium | Done |
| P2 | Split `permission/engine.py` | Improves readability | Low | Done |
| P2 | Split `config.py` into package | Reduces god-module risk | Medium | Done |
| P3 | Split remaining 500+ line files | Code hygiene | Low each | Done |
| P3 | SlashHandler protocol extraction | Refactoring safety | Low | Done |

## 6. Verification

Commands run on 2026-06-06:

```
.venv/bin/python -m pytest tests/test_config.py tests/test_agent/test_core_flow.py tests/test_agent/test_run_loop.py tests/test_agent/test_stream_llm.py tests/test_ui_events.py tests/test_ui_gateway.py tests/test_ui_frontend_protocol.py -q
# 118 passed

.venv/bin/python -m pytest tests/test_agent/test_slash_model.py tests/test_agent/test_slash_mcp.py tests/test_agent/test_slash_skills.py tests/test_lsp.py tests/test_agent/test_run_loop.py -q
# 45 passed

.venv/bin/python -m pytest tests/ -v
# 554 passed

rg -n "from voidx\.ui|import voidx\.ui" src/voidx/agent src/voidx/tools src/voidx/memory src/voidx/permission
# no matches

rg -n "from voidx\.agent|import voidx\.agent" src/voidx/tools src/voidx/memory src/voidx/permission
# no matches

find src/voidx -name '*.py' -not -path '*/__pycache__/*' -print0 | xargs -0 wc -l | awk '$2 != "total" && $1 >= 500 {print}'
# no matches

.venv/bin/python -c "import sys; import voidx.agent.graph; print(any(m.startswith('voidx.ui') for m in sys.modules)); print([m for m in sys.modules if m.startswith('voidx.ui')][:10])"
# False
# []
```
