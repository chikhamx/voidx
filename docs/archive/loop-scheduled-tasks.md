# /loop: Session-Scoped Scheduled Prompt Execution

> **Status: Done** — Archived on 2026-07-13.

**Date:** 2026-07-11  
**Status:** Design / Awaiting approval — not implemented

## TL;DR

A `/loop` slash command re-runs a prompt on a fixed or dynamic interval within the current session. The prompt can be plain text, a `@file.md` reference, or a workspace-local `@script` reference whose stdout becomes the prompt. A new `schedule_wakeup` tool lets the main agent set the next dynamic trigger. One loop is active per session; starting a new loop replaces the old one, and changing sessions stops the current loop.

## Document State and Review Gate

This document is a pre-implementation technical design. References to current behavior and code paths are evidence for the problem and architectural boundaries; statements using "must", "will", "add", or "target" describe changes that do not exist yet.

Design approval does not require the proposed fields, helpers, files, or tests to already be present. The design quality gate is:

- current behavior and referenced implementation boundaries are accurate;
- target behavior, ownership, ordering, cleanup, and failure semantics are unambiguous;
- every target change has a concrete source path and deterministic test coverage;
- the implementation order is dependency-correct; and
- the post-implementation verification commands are complete.

## Context

voidx already has `VoidXGraph.run_synthetic_turn(text)` in `src/voidx/agent/graph/core/voidx_graph.py`, which injects a synthetic user message and runs a full agent turn. The slash command system dispatches `/` commands through `SlashHandler` and mixins in `src/voidx/agent/slash/`. The `@` attachment parser in `src/voidx/agent/attachments.py` already recognizes `@file` and `@"quoted path"` tokens.

What's missing is a scheduler that:

1. Waits for a configured interval.
2. Resolves the prompt source at each trigger.
3. Waits until no agent turn is currently running.
4. Calls `run_synthetic_turn(prompt)` to inject the prompt.
5. Repeats until stopped or the session changes.

## Usage

```text
# Fixed interval + plain text
/loop 5m check if the build finished and tell me

# Fixed interval + markdown file
/loop 10m @docs/review-checklist.md

# Fixed interval + script; stdout becomes prompt
/loop 5m @scripts/gen-status.sh

# Dynamic interval; agent decides next trigger via schedule_wakeup
/loop check the deploy status

# Stop the current loop
/loop stop

# Show current loop status
/loop status
```

### Interval Syntax

| Form | Example | Parsed interval |
|------|---------|-----------------|
| Leading token | `/loop 30m check the build` | every 30 minutes |
| Trailing every clause | `/loop check the build every 2h` | every 2 hours |
| No interval | `/loop check the build` | dynamic mode, default 10m, agent-adjustable |

Supported units: `s`, `m`, `h`, `d`. Values below 60 seconds are rounded up to 60 seconds. Minimum interval: 1 minute.

## Architecture

### New module: `src/voidx/agent/loop/`

```text
src/voidx/agent/loop/
├── __init__.py          # exports LoopManager and PromptSource
├── manager.py           # LoopManager — asyncio.Task scheduler and lifecycle state
├── prompt_source.py     # PromptSource — resolves text/file/script into prompt text
└── slash.py             # SlashLoopMixin — /loop command handler
```

### New tool: `src/voidx/tools/schedule_wakeup.py`

`ScheduleWakeupTool` lets the main agent set the next dynamic loop delay or stop the loop.

## Component Responsibilities

### PromptSource (`src/voidx/agent/loop/prompt_source.py`)

`PromptSource` stores the raw `/loop` prompt argument after interval parsing and resolves it at each trigger, not only at `/loop` creation time. This lets referenced files or scripts reflect current state.

```python
class PromptSource:
    raw: str
    kind: Literal["text", "file", "script", "mixed"]
    references: list[PathReference]

    async def resolve(
        self,
        workspace: str,
        *,
        bash_tool: BaseTool | None = None,
        ctx: ToolContext | None = None,
    ) -> str:
        """Return prompt text for this iteration."""
```

Resolution rules:

1. Reuse the attachment token grammar from `src/voidx/agent/attachments.py` for `@path` and `@"quoted path"` recognition. The implementation may expose or wrap the existing private tokenizer, but tests must lock the grammar.
2. Resolve every referenced path relative to `workspace`, after `Path.resolve()`, and reject references outside the workspace before reading or executing.
3. Only regular files are valid prompt sources. Directories, symlinks escaping the workspace, missing files, and device/special files produce a structured loop error prompt.
4. File mode reads UTF-8 text with replacement for invalid bytes and applies a size cap matching `MAX_TEXT_ATTACHMENT_BYTES` unless a separate loop-specific cap is introduced.
5. Script mode is selected only for workspace-local regular files that are executable, have a shebang, or have an allowlisted script extension such as `.sh` or `.py`.
6. Script mode runs through the existing shell tool and permission/sandbox system; it must not use raw `asyncio.create_subprocess_exec`.
7. Script command construction must shell-quote the resolved script path because `BashTool` executes a shell command string. Paths containing spaces or shell metacharacters must be covered by tests.
8. Executable/shebang scripts may be invoked directly with a quoted path. Extension-only scripts that are not executable must be invoked through an allowlisted interpreter with the script path as a quoted argument, e.g. `bash {quoted}` for `.sh` or `python {quoted}` for `.py`, if that interpreter choice is accepted for v1.
9. Mixed mode preserves the literal text around each `@` reference and replaces each reference with its resolved file content or script stdout. Multiple references are allowed only if the implementation covers ordering and error behavior with tests; otherwise v1 should reject multiple references with a clear message.
10. Empty resolved prompt text becomes a loop error prompt such as `[loop] prompt source produced no output: {display_path}` rather than injecting an empty user turn.
11. Script failure, timeout, sandbox denial, or permission denial becomes a loop error prompt that includes the `ToolResult.output` summary. The failing output is not silently treated as a successful task prompt.

Script execution contract:

```python
quoted = shlex.quote(str(resolved_script_path))
command = quoted if invoke_directly else f"{interpreter} {quoted}"
result = await bash_tool.execute({"command": command, "timeout": 30}, ctx)
```

The cwd is `ctx.workspace`, inherited from `BashTool` behavior.

### LoopManager (`src/voidx/agent/loop/manager.py`)

`LoopManager` owns one scheduler task per graph/session and exposes start, stop, status, wakeup, and cleanup operations.

```python
class LoopManager:
    def __init__(
        self,
        host: GraphRunLoopHost,
        *,
        idle_event: asyncio.Event,
        default_interval_seconds: float = 600,
    ) -> None: ...

    def start(
        self,
        prompt_source: PromptSource,
        interval_seconds: float | None,
        *,
        bash_tool: BaseTool | None = None,
        ctx: ToolContext | None = None,
        session_id: str | None = None,
    ) -> None: ...

    def stop(self) -> None: ...
    def status(self) -> dict | None: ...
    def schedule_wakeup(self, delay_seconds: float | None = None, *, stop: bool = False) -> None: ...
    async def cleanup(self) -> None: ...
```

Scheduling rules:

1. `start()` cancels any existing loop before creating a new `asyncio.Task`.
2. Fixed mode sleeps `interval_seconds` before each iteration.
3. Dynamic mode sleeps the current wakeup delay, or the default 10 minutes when no one has scheduled a delay.
4. `schedule_wakeup(delay_seconds)` affects the *next unslept dynamic interval*. If the loop is already sleeping, the sleep must be interruptible so a newly scheduled earlier delay takes effect promptly. Use an `asyncio.Event`, timeout wait, or a task restart; a plain `_wakeup_delay` field read only before sleep is not sufficient.
5. Each scheduled dynamic delay is consumed once, then falls back to the default unless the agent schedules another delay.
6. `schedule_wakeup(stop=True)` cancels the active loop. `delay_seconds` may be omitted when `stop=True`.
7. After the sleep completes, the loop awaits `idle_event.wait()` and fires once. Missed intervals while the agent is busy are not replayed.
8. The manager must guard against self-overlap: it must not start a second synthetic turn while a previous loop-triggered turn is still running.
9. `status()` returns enough structured data for `/loop status`: active flag, mode, interval/default delay, next-fire estimate when known, prompt display summary, session id, and last error if any.
10. `cleanup()` cancels the task and awaits cancellation so tests and shutdown do not leak tasks.

Pseudo-flow:

```python
async def _run_loop(self):
    while not cancelled:
        delay = self._next_delay()
        await self._sleep_interruptibly(delay)
        await self._idle_event.wait()
        prompt = await self._prompt_source.resolve(
            self._workspace,
            bash_tool=self._bash_tool,
            ctx=self._ctx,
        )
        await self._host.run_synthetic_turn(prompt, display_text=self._display_text(prompt))
```

### Idle Coordination

The idle event belongs to `GraphTurnRunner`, because `GraphTurnRunner.run_once()` is the single path used by both normal user turns and `VoidXGraph.run_synthetic_turn()`.

Target changes:

- `src/voidx/agent/graph/turn_runner.py`
  - Add `self.idle_event = asyncio.Event()` in `GraphTurnRunner.__init__` and set it initially.
  - In `run_once()`, clear `idle_event` immediately after `host._usage_stats.begin_turn()` and before user-message/session work starts.
  - In the existing `finally` block, set `idle_event` after usage stats and UI/session cleanup that must run while the turn is still considered busy.
- `src/voidx/agent/graph/core/voidx_graph.py`
  - Initialize `LoopManager(self, idle_event=self._turn_runner.idle_event)` after creating the turn runner.
  - Expose a `loop_manager` property for host protocols and tools.
- `src/voidx/agent/graph/turn_mixin.py`
  - If tests or legacy mixins instantiate a temporary `GraphTurnRunner`, ensure they expose/use the same runner instance for idle state or document the no-op fallback. Add a regression test for this path if it remains supported.

This removes the previous ambiguity about whether `_idle_event` lives on `VoidXGraph` or `GraphTurnRunner`.

### ScheduleWakeupTool (`src/voidx/tools/schedule_wakeup.py`)

```python
class ScheduleWakeupInput(BaseModel):
    delay_seconds: float | None = Field(
        default=None,
        description="Seconds until the next dynamic loop iteration. Min 60, max 3600. Optional when stop=true.",
    )
    stop: bool = Field(
        default=False,
        description="Set true to stop the current loop instead of scheduling the next wakeup.",
    )
```

Behavior:

- If `ctx.loop_manager` is missing, return `ToolResult(metadata={"error": True})` explaining that no session loop is available.
- If there is no active dynamic loop, return a clear non-fatal error. The tool should not silently create a loop.
- Clamp or reject invalid delays consistently. Preferred behavior: reject values outside `[60, 3600]` with a validation error so the agent sees the exact problem. If clamping is chosen, the response must state the effective delay.
- `stop=True` stops both fixed and dynamic loops.
- Return structured metadata: `scheduled`, `stopped`, `delay_seconds`, `loop_active`, and `mode`.

### ToolContext Integration (`src/voidx/tools/base.py` and executor)

Add an excluded field to `ToolContext`:

```python
loop_manager: Any | None = Field(default=None, exclude=True)
```

Implementation constraints:

- Preserve the current `ToolContext.__init__` behavior that pops and stores `file_mtimes`, `file_read_coverage`, and `workflow_repeat_tracker` by reference. Adding `loop_manager` must not deep-copy or break those shared mutable objects.
- In `src/voidx/agent/graph/tool_executor/executor.py`, pass `loop_manager=getattr(host, "loop_manager", None)` or `getattr(host, "_loop_manager", None)` inside `make_context()` alongside `mcp_manager` and `lsp_manager`.
- Add a regression test that constructs `ToolContext` through `make_context()` and verifies both `loop_manager` and the existing shared mutable fields remain available.

### Slash Integration (`src/voidx/agent/loop/slash.py` and `src/voidx/agent/slash/`)

`SlashLoopMixin` handles `/loop` while staying behind the slash host boundary.

Parsing logic:

1. Empty args or `help` prints usage.
2. `stop` calls `host.loop_manager.stop()` and prints confirmation.
3. `status` prints structured status from `host.loop_manager.status()`.
4. Parse leading interval (`\d+[smhd]`) or trailing `every \d+[smhd]`.
5. Remaining text becomes `PromptSource.raw`; empty remaining text is rejected.
6. Build or fetch the shell tool instance and a minimal `ToolContext` for script resolution. The context must include current workspace, permission state, sandbox state, grants callbacks, and `loop_manager`.
7. Call `host.loop_manager.start(prompt_source, interval_seconds, bash_tool=..., ctx=..., session_id=host.session.id if present)`.

Target changes:

- `src/voidx/agent/slash/handler.py`
  - Add `SlashLoopMixin` to the `SlashHandler` base classes.
  - Register `"/loop": lambda: self._loop(args)` in the handlers dict.
- `src/voidx/agent/slash/host.py`
  - Add `loop_manager` to `SlashCommandHost`.
  - Add `SlashHostAdapter.loop_manager` that returns `raw.loop_manager` or `raw._loop_manager`.
  - Keep the adapter safe for older tests: if missing, `/loop` should print a clear unsupported message rather than raising `AttributeError`.

### Graph Host and Session Lifecycle

Target changes:

- `src/voidx/agent/graph/contracts.py`
  - Add `loop_manager` to `GraphRunLoopHost` or any host protocol used by tools/runner.
- `src/voidx/agent/graph/core/voidx_graph.py`
  - Initialize `self._loop_manager` after `self._turn_runner` is created.
  - Expose `@property def loop_manager(self) -> LoopManager`.
  - Call `await self._loop_manager.cleanup()` before clearing session state in `clear_current_session()`.
  - Stop or cleanup the loop before `resume_session()` switches to another session. v1 policy: loops are session-scoped and never migrate across sessions.
- `src/voidx/agent/slash/host.py`
  - Adapter methods that clear or resume sessions must preserve the above cleanup behavior by calling the graph methods, not bypassing them.

Lifecycle policy:

| Event | Behavior |
|-------|----------|
| New `/loop ...` | Cancels and replaces existing loop |
| `/loop stop` | Cancels active loop |
| `/loop status` | Reports active loop or "no active loop" |
| `/clear` / `clear_current_session()` | Cancels active loop before session reset |
| `/resume` / `resume_session()` | Cancels active loop before switching session |
| Process exit | Event loop cancellation may stop tasks, but `cleanup()` remains the explicit tested path |
| Restart | No persistence; loop is not restored |

### Tool Registration and Subagent Semantics

Target changes:

- `src/voidx/tools/registry.py`
  - Add `loop_manager=None` to `ToolRegistry.__init__`, store `self._loop_manager`.
  - Register `ScheduleWakeupTool` with access to the loop manager if the constructor needs it; otherwise the tool may rely solely on `ctx.loop_manager`.
  - Update `filtered_copy()` to preserve `loop_manager`, e.g. `ToolRegistry(settings=self._settings, tracker=self._tracker, loop_manager=self._loop_manager)`, before replacing `_tools` and `_instances`.
- `src/voidx/agent/graph/wiring.py`
  - Pass `loop_manager` after `VoidXGraph` creates the manager, or provide a post-construction injection method if registry construction currently happens earlier.
- `src/voidx/agent/graph/subagent.py`
  - Define v1 behavior explicitly: `schedule_wakeup` is only intended for the main agent loop. Child agents must either not receive the tool in their filtered tool list or receive it with a clear error saying wakeup scheduling is unavailable from subagents.

Preferred v1: keep `schedule_wakeup` available only to the main agent. This avoids subagents accidentally changing the parent session's recurring schedule.

### Command Catalog

Target file: `src/voidx/ui/commands.py`

Add entries:

- `("/loop", "Run a prompt on a recurring interval")`
- `("/loop stop", "Stop the current loop")`
- `("/loop status", "Show current loop status")`

If command catalog metadata in `src/voidx/ui/command_catalog.py` needs direct-run or dangerous-command classification, `/loop stop` and `/loop status` may be direct-run; `/loop ...` should follow normal slash dispatch and must not be marked dangerous solely because scripts may be referenced. Script execution still goes through tool permissions.

## Behavior Specification

### Fixed Interval Mode

1. User runs `/loop 5m check the build`.
2. Slash handler parses a 300-second fixed interval and a text prompt source.
3. `LoopManager.start()` cancels any existing loop and creates a new task.
4. The task sleeps 300 seconds, waits for idle, resolves the prompt, and calls `run_synthetic_turn("check the build")`.
5. The loop repeats until stopped, session changes, or cleanup runs.

### Dynamic Interval Mode

1. User runs `/loop check the deploy status` with no interval.
2. `LoopManager.start()` creates a dynamic loop with the default 10-minute delay.
3. After a loop-triggered turn, the main agent may call `schedule_wakeup(delay_seconds=120)`.
4. The manager schedules the next dynamic iteration for 120 seconds after the current turn becomes idle, unless another valid wakeup supersedes it before sleep starts or the implementation supports interrupting the sleep.
5. If the agent does not call `schedule_wakeup`, the default 10-minute delay is used.
6. The agent can call `schedule_wakeup(stop=True)` to end the loop.

### Prompt Source Resolution Examples

| Input | Kind | Resolution |
|-------|------|------------|
| `check the build` | text | Literal string |
| `@docs/review.md` | file | Current file content |
| `@scripts/status.sh` | script | Execute safely through `BashTool`, capture stdout |
| `Check this: @docs/review.md` | mixed | Literal prefix plus file content |

### Error Handling

| Case | Behavior |
|------|----------|
| File not found | Inject loop error prompt and record `last_error` |
| Outside-workspace path | Reject resolution; do not read or execute |
| Directory/special file | Reject resolution |
| Script timeout/failure/denial | Inject loop error prompt containing tool output summary |
| Empty script stdout | Inject loop error prompt |
| Agent busy at fire time | Wait for idle and fire once; no catch-up burst |
| Invalid `/loop` interval | Print validation error; do not start loop |
| Invalid `schedule_wakeup` delay | Return tool validation error or explicit clamp result |
| Missing loop manager | Slash command/tool reports unsupported instead of crashing |

## Implementation Order

1. `src/voidx/agent/loop/prompt_source.py` — path parsing, workspace containment, file/script resolution, shell quoting tests.
2. `src/voidx/agent/loop/manager.py` — scheduler, interruptible dynamic wakeup, status, cleanup.
3. `src/voidx/agent/graph/turn_runner.py` — add and maintain `idle_event` around `run_once()`.
4. `src/voidx/agent/graph/core/voidx_graph.py` — initialize/expose `LoopManager`; cleanup on clear/resume.
5. `src/voidx/agent/graph/contracts.py` — expose `loop_manager` on host protocols.
6. `src/voidx/tools/base.py` — add `ToolContext.loop_manager` without breaking shared mutable context fields.
7. `src/voidx/agent/graph/tool_executor/executor.py` — inject `loop_manager` in `make_context()`.
8. `src/voidx/tools/schedule_wakeup.py` — implement tool validation and manager calls.
9. `src/voidx/tools/registry.py` and `src/voidx/agent/graph/wiring.py` — register the tool and preserve `loop_manager` through `filtered_copy()`.
10. `src/voidx/agent/graph/subagent.py` — enforce main-agent-only schedule wakeup behavior.
11. `src/voidx/agent/loop/slash.py` — implement `/loop` parsing and user output.
12. `src/voidx/agent/slash/host.py` and `src/voidx/agent/slash/handler.py` — expose host access and register the slash command.
13. `src/voidx/ui/commands.py` and, if needed, `src/voidx/ui/command_catalog.py` — add command palette entries.

## Verification

### Unit Tests

```bash
# PromptSource: text/file/script/mixed, workspace containment, quoting, empty output, errors
./test.py --backend -- src/tests/test_agent/loop/test_prompt_source.py -v

# LoopManager: fixed mode, dynamic mode, interruptible wakeup, no catch-up, stop, cleanup, status
./test.py --backend -- src/tests/test_agent/loop/test_manager.py -v

# ScheduleWakeupTool: missing manager, fixed/dynamic behavior, invalid delays, stop=true
./test.py --backend -- src/tests/test_tools/test_schedule_wakeup.py -v

# SlashLoopMixin and host adapter boundary
./test.py --backend -- src/tests/test_agent/slash/test_slash_loop.py -v

# ToolContext/registry integration: loop_manager injection, shared fields preserved, filtered_copy
./test.py --backend -- src/tests/test_tools/test_tool_context_loop_manager.py src/tests/test_tools/test_tool_registry.py -v
```

### Integration Test

```bash
./test.py --backend -- src/tests/test_agent/loop/test_integration.py -v
```

Integration coverage:

1. `/loop 1m test prompt` creates a loop task.
2. Loop fires after interval and calls `run_synthetic_turn` once.
3. `/loop stop` cancels the loop.
4. New `/loop` replaces the existing loop.
5. `schedule_wakeup` adjusts the next dynamic interval and does not wait for a stale default sleep.
6. Session clear cancels the loop.
7. Session resume cancels the loop and does not migrate it to the new session.
8. Script paths with spaces or shell metacharacters are quoted and do not execute unintended commands.
9. Child agents cannot mutate the parent loop schedule in v1.

### Manual Smoke Test

```bash
./python.py -m voidx.main
# In the session:
/loop 1m echo hello
# Wait 1 minute — agent should receive "echo hello" as a synthetic turn
/loop status
# Should show: active, interval=60s, next fire in roughly the remaining delay
/loop stop
# Should show: loop stopped
```

### Final Verification Command

After implementation, run the focused tests above first, then:

```bash
./test.py --backend -- src/tests/test_agent/loop src/tests/test_agent/slash/test_slash_loop.py src/tests/test_tools/test_schedule_wakeup.py -v
```
