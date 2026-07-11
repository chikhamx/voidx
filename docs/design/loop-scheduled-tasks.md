# /loop: Session-Scoped Scheduled Prompt Execution

**Date:** 2026-07-11  
**Status:** Design / Awaiting approval — not implemented

## TL;DR

A `/loop` slash command that re-runs a prompt on a fixed or dynamic interval within the
current session. The prompt can be plain text, a `@file.md` reference (reusing the
existing attachment syntax), or a `@script` reference (executed via bash, stdout becomes
the prompt). A new `schedule_wakeup` tool lets the agent self-pace the next trigger in
dynamic mode. One loop per session; new loop replaces the old one.

## Document State and Review Gate

This document is a pre-implementation technical design. References to current behavior and
code paths are evidence for the problem and architectural boundaries; statements using
"must", "will", "add", or "target" describe changes that do not exist yet.

Design approval does not require the proposed fields, helpers, files, or tests to already
be present. The design quality gate is:

- current behavior and referenced implementation boundaries are accurate;
- target behavior, ownership, ordering, cleanup, and failure semantics are unambiguous;
- every target change has a concrete source path and deterministic test coverage;
- the implementation order is dependency-correct; and
- the post-implementation verification commands are complete.

## Context

voidx already has `run_synthetic_turn(text)` on `VoidXGraph` (src/voidx/agent/graph/core/voidx_graph.py:468)
which injects a synthetic user message and runs a full agent turn. The slash command system
(`SlashHandler` in src/voidx/agent/slash/handler.py) dispatches `/` commands via mixin
pattern. The `@` attachment syntax (src/voidx/agent/attachments.py:20) already parses
`@file` and `@"quoted path"` references into file contents.

What's missing is a scheduler that:

1. Waits for a configured interval.
2. Resolves the prompt source (text / file / script).
3. Waits for the agent to be idle (not mid-turn).
4. Calls `run_synthetic_turn(prompt)` to inject the prompt.
5. Repeats.

## Usage

```
# Fixed interval + plain text
/loop 5m check if the build finished and tell me

# Fixed interval + markdown file (reuses @ syntax)
/loop 10m @docs/review-checklist.md

# Fixed interval + script (executed via bash, stdout = prompt)
/loop 5m @scripts/gen-status.sh

# Dynamic interval (agent decides next trigger via schedule_wakeup tool)
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
| No interval | `/loop check the build` | dynamic mode (default 10m, agent-adjustable) |

Supported units: `s` (seconds, rounded up to 1m), `m` (minutes), `h` (hours), `d` (days).
Minimum interval: 1 minute.

## Architecture

### New module: `src/voidx/agent/loop/`

```
src/voidx/agent/loop/
├── __init__.py          # exports LoopManager
├── manager.py           # LoopManager — asyncio.Task scheduler
├── prompt_source.py     # PromptSource — resolve text/file/script to prompt string
└── slash.py             # SlashLoopMixin — /loop command handler
```

### New tool: `src/voidx/tools/schedule_wakeup.py`

`ScheduleWakeupTool` — lets the agent set the next trigger delay or stop the loop.

### Component responsibilities

#### PromptSource (`prompt_source.py`)

Resolves the raw `/loop` argument into a prompt string at each trigger.

```python
class PromptSource:
    raw: str  # original argument after interval parsing
    kind: Literal["text", "file", "script"]
    path: str | None  # resolved path for file/script

    async def resolve(self, workspace: str, bash_tool: BaseTool | None = None, ctx: ToolContext | None = None) -> str:
        """Return the prompt text for this iteration.

        For script mode, bash_tool and ctx must be provided so the script
        runs through the permission/sandbox system.
        """
```

Resolution logic:

1. **Detect `@` reference**: reuse `_attachment_tokens` from `attachments.py` to find
   `@path` tokens in the raw argument.
2. **File mode**: if the `@` reference resolves to a `.md` or text file, read its content
   as the prompt.
3. **Script mode**: if the `@` reference resolves to an executable file (has shebang or
   `.sh`/`.py` extension), execute it via the existing `BashTool` (src/voidx/tools/bash/tool.py)
   to ensure the command passes through `_check_command` (safety), `_sandbox_denial`
   (sandbox enforcement), and the permission system. `PromptSource.resolve()` receives a
   `BashTool` instance and `ToolContext` from the `LoopManager`, calls
   `bash_tool.execute({"command": script_path}, ctx)`, and extracts `ToolResult.output`
   as the prompt. This avoids bypassing the permission/sandbox layer with a raw
   `asyncio.create_subprocess_exec`.
4. **Text mode**: if no `@` reference, the raw argument is the prompt verbatim.
5. **Mixed mode**: if the argument contains both text and `@` references, the text is
   prepended to the file/script output.

#### LoopManager (`manager.py`)

```python
class LoopManager:
    def __init__(self, host: GraphRunLoopHost) -> None:
        self._bash_tool: BaseTool | None = None  # injected by host at start()
        self._ctx: ToolContext | None = None     # injected by host at start()

    def start(
        self,
        prompt_source: PromptSource,
        interval_seconds: float | None,  # None = dynamic mode
        *,
        bash_tool: BaseTool | None = None,
        ctx: ToolContext | None = None,
    ) -> None:
        """Cancel any existing loop, then start a new one.

        bash_tool and ctx are stored for script-mode prompt resolution.
        """

    def stop(self) -> None:
        """Cancel the current loop."""

    def status(self) -> dict | None:
        """Return current loop state or None."""

    def schedule_wakeup(self, delay_seconds: float, *, stop: bool = False) -> None:
        """Called by ScheduleWakeupTool to set next trigger (dynamic mode)."""

    async def cleanup(self) -> None:
        """Cancel loop task — called on session clear/exit."""
```

Internal scheduling loop:

```
async def _run_loop(self):
    while not cancelled:
        if interval is fixed:
            await asyncio.sleep(interval_seconds)
        else:  # dynamic mode
            if self._wakeup_delay is not None:
                delay = self._wakeup_delay
                self._wakeup_delay = None  # consume, reset to default next time
            else:
                delay = self._default_interval  # 10m fallback
            await asyncio.sleep(delay)

        # Wait for agent idle (run_once sets _idle_event in finally block)
        await self._idle_event.wait()

        # Resolve prompt (bash_tool + ctx needed for script mode)
        prompt = await prompt_source.resolve(workspace, bash_tool=self._bash_tool, ctx=self._ctx)

        # Inject synthetic turn — this enters run_once which clears _idle_event
        await host.run_synthetic_turn(prompt, display_text=f"[loop] {prompt[:80]}")
```

**Dynamic mode wakeup read-reset timing**: `LoopManager` holds a `_wakeup_delay: float | None`
field (not an `asyncio.Event`). The `ScheduleWakeupTool` calls
`loop_manager.schedule_wakeup(delay_seconds)` which sets `self._wakeup_delay = delay_seconds`.
The loop reads `_wakeup_delay` **before** sleeping, then resets it to `None` (consume).
This means:

1. If the agent calls `schedule_wakeup(120)` during a turn, `_wakeup_delay` is set to 120.
2. After the turn ends, `_idle_event` is set, the loop wakes up.
3. The loop reads `_wakeup_delay=120`, resets it to `None`, sleeps 120s.
4. Next iteration: `_wakeup_delay` is `None`, so the default 10m interval is used — unless
   the agent calls `schedule_wakeup` again during that turn.
5. If the agent calls `schedule_wakeup(stop=true)`, `LoopManager.stop()` is called, which
   cancels the loop task.

This read-before-sleep-consume pattern ensures the agent's requested delay is used exactly
once, then falls back to the default — matching Claude Code's `ScheduleWakeup` semantics.

**Idle coordination (new mechanism)**: The manager uses a new `asyncio.Event`
(`_idle_event`) that is set when the agent is not executing a turn and cleared when a turn
starts. This event does not exist yet — it must be added to `GraphTurnRunner`
(src/voidx/agent/graph/turn_runner.py:95) as an instance field, and the set/clear calls
must be inserted into `run_once` (src/voidx/agent/graph/turn_runner.py:98) at the
following target locations:

- **Clear** (`_idle_event.clear()`) — insert after `host._usage_stats.begin_turn()` at
  line 109, before the `try` block at line 111 — marks the agent as busy.
- **Set** (`_idle_event.set()`) — insert in the `finally` block (line 431), after
  `host._usage_stats.end_turn()` at line 432 — marks the agent as idle again.

The event is created in `GraphTurnRunner.__init__` (line 95) as `self._idle_event =
asyncio.Event()`, initially set (agent starts idle).

This covers both user-initiated turns and loop-injected `run_synthetic_turn` calls, since
both go through `run_once`. When the loop fires, it calls `run_synthetic_turn`, which
enters `run_once`, which clears the event — preventing re-entrant loop triggers. After
the turn completes, the `finally` block sets the event, unblocking the next loop
iteration's `await self._idle_event.wait()`.

**No catch-up for missed fires**: If the agent is busy when the interval elapses, the
loop waits for idle, then fires once — not once per missed interval.

#### ScheduleWakeupTool (`schedule_wakeup.py`)

```python
class ScheduleWakeupInput(BaseModel):
    delay_seconds: float = Field(
        description="Seconds until the next loop iteration. Min 60, max 3600."
    )
    stop: bool = Field(
        default=False,
        description="Set to true to stop the loop instead of scheduling the next wakeup.",
    )

class ScheduleWakeupTool(BaseTool):
    id = "schedule_wakeup"
    description = (
        "Reschedule the next iteration of a self-paced /loop. "
        "Call this at the end of each loop iteration to pick when the next one runs. "
        "Pass stop=true to end the loop."
    )
```

The tool reads `loop_manager` from `ToolContext` and calls
`loop_manager.schedule_wakeup(delay_seconds, stop=stop)`.

**Host access via ToolContext (Option A)**: Add `loop_manager` as an optional field on
`ToolContext` (src/voidx/tools/base.py:87). The field must use `exclude=True` to prevent
Pydantic from attempting serialization of the non-serializable `LoopManager` instance,
consistent with how `interact` and `add_extra_path` are already handled:

```python
# In ToolContext (src/voidx/tools/base.py), add:
loop_manager: Any | None = Field(default=None, exclude=True)
```

The field is populated in `make_context()` (src/voidx/agent/graph/tool_executor/executor.py:108),
alongside the existing `mcp_manager` and `lsp_manager` injections:

```python
# In make_context() at executor.py:113, add:
loop_manager=getattr(host, "_loop_manager", None),
```

This approach is preferred over Option B (storing the wakeup request in
`ToolResult.metadata` and letting the graph host apply it post-execution) because the
tool can validate the delay and give immediate feedback to the agent.

#### SlashLoopMixin (`slash.py`)

```python
class SlashLoopMixin:
    async def _loop(self, args: str) -> None:
        """Handle /loop command."""
```

Parsing logic:

1. `args == "stop"` → `host.loop_manager.stop()`
2. `args == "status"` → print current loop status
3. Parse interval (leading `\d+[smhd]` or trailing `every \d+[smhd]`)
4. Remaining text after interval removal = prompt source
5. Create `PromptSource` from remaining text
6. `host.loop_manager.start(prompt_source, interval_seconds, bash_tool=..., ctx=...)`

### Integration points

#### 1. SlashHandler registration

**File**: `src/voidx/agent/slash/handler.py`

- Add `SlashLoopMixin` to `SlashHandler` base classes.
- Add `"/loop": lambda: self._loop(args)` to the `handlers` dict.

#### 2. Tool registration

**File**: `src/voidx/tools/registry.py`

- Import `ScheduleWakeupTool`.
- Add `loop_manager=None` parameter to `ToolRegistry.__init__` (src/voidx/tools/registry.py:36),
  stored as `self._loop_manager` (same pattern as `tracker`).
- Register in `_register_builtins` with `ScheduleWakeupTool(loop_manager=self._loop_manager)`
  (same injection pattern as `TodoWriteTool(tracker=self._tracker)` at registry.py:63).
- Update all `ToolRegistry(...)` call sites to pass `loop_manager=host.loop_manager`.

#### 3. Graph host

**File**: `src/voidx/agent/graph/contracts.py`

- Add `loop_manager: LoopManager | None` to `GraphRunLoopHost` protocol.

**File**: `src/voidx/agent/graph/core/voidx_graph.py`

- Initialize `self._loop_manager = LoopManager(self)` in `__init__`.
- Expose via `@property loop_manager`.
- Set/clear `_idle_event` around `_run_once` calls (in `run_once` or
  `run_synthetic_turn`).
- Call `await self._loop_manager.cleanup()` in `clear_current_session`.

#### 4. ToolContext

**File**: `src/voidx/tools/base.py`

- Add `loop_manager: Any | None = Field(default=None, exclude=True)` to `ToolContext`.

**File**: `src/voidx/agent/graph/tool_executor/executor.py` (or wherever context is built)

- Set `ctx.loop_manager = host.loop_manager` when building `ToolContext`.

#### 5. Command catalog

**File**: `src/voidx/ui/commands.py`

- Add entries:
  - `("/loop", "Run a prompt on a recurring interval")`
  - `("/loop stop", "Stop the current loop")`
  - `("/loop status", "Show current loop status")`

## Behavior specification

### Fixed interval mode

1. User runs `/loop 5m check the build`.
2. `LoopManager.start()` cancels any existing loop, creates a new `asyncio.Task`.
3. The task sleeps 5 minutes, waits for agent idle, resolves the prompt, calls
   `run_synthetic_turn("check the build")`.
4. Repeats until `/loop stop` or session ends.

### Dynamic interval mode

1. User runs `/loop check the deploy status` (no interval).
2. `LoopManager.start()` creates a task with `interval_seconds=None`.
3. The task sleeps the default 10 minutes, waits for idle, injects the prompt.
4. During the agent's turn, the agent may call `schedule_wakeup(delay_seconds=120)`.
5. After the turn completes, the loop uses 120 seconds as the next interval instead
   of the default.
6. If the agent does not call `schedule_wakeup`, the default 10-minute interval is used.
7. The agent can call `schedule_wakeup(stop=true)` to end the loop.

### Prompt source resolution

| Input | Kind | Resolution |
|-------|------|------------|
| `check the build` | text | Literal string |
| `@docs/review.md` | file | Read file content |
| `@scripts/status.sh` | script | Execute, capture stdout |
| `Check this: @docs/review.md` | mixed | "Check this:" + file content |

Script execution details:

- Executed via `BashTool.execute({"command": script_path, "timeout": 30}, ctx)` — passes
  through `_check_command` (safety), `_sandbox_denial` (sandbox), and permission system.
- 30-second timeout (BashTool's default timeout parameter); on timeout, `ToolResult.output`
  contains the timeout error message, which becomes the prompt.
- Script path must resolve within workspace (enforced by `_sandbox_denial`).
- The script runs with the same environment and cwd as the voidx process (via `ctx.workspace`).

### Session lifecycle

- **New loop replaces old**: `start()` always cancels the existing loop first.
- **Session clear**: `clear_current_session()` calls `loop_manager.cleanup()`.
- **Session exit**: loop task is cancelled when the event loop shuts down.
- **No persistence**: loop does not survive session restart (unlike Claude Code's
  `--resume` restoration — can be added later).

### Error handling

- **Script failure**: `BashTool` returns a `ToolResult` with `metadata.error=True` and
  the error in `output`; that output becomes the prompt — agent sees the error and can act on it.
- **File not found**: prompt becomes `"[loop] file not found: {path}"`.
- **Agent busy**: loop waits for idle, fires once (no catch-up).
- **schedule_wakeup with invalid delay**: clamped to [60, 3600] seconds.

## Implementation order

1. `src/voidx/agent/loop/prompt_source.py` — no dependencies, testable in isolation.
2. `src/voidx/agent/loop/manager.py` — depends on `PromptSource` and host protocol.
3. `src/voidx/tools/schedule_wakeup.py` — depends on `ToolContext` change.
4. `src/voidx/tools/base.py` — add `loop_manager` field to `ToolContext`.
5. `src/voidx/tools/registry.py` — add `loop_manager` param to `__init__`, register `ScheduleWakeupTool`, update call sites.
6. `src/voidx/agent/loop/slash.py` — depends on `LoopManager` and `PromptSource`.
7. `src/voidx/agent/slash/handler.py` — wire `SlashLoopMixin`.
8. `src/voidx/agent/graph/contracts.py` — add `loop_manager` to protocol.
9. `src/voidx/agent/graph/turn_runner.py` — add `_idle_event` field to `GraphTurnRunner`, insert clear/set calls in `run_once`.
10. `src/voidx/agent/graph/core/voidx_graph.py` — initialize `LoopManager`, wire `_idle_event` to `LoopManager`, cleanup on session clear.
11. `src/voidx/agent/graph/tool_executor/executor.py` — pass `loop_manager` to context.
12. `src/voidx/ui/commands.py` — add command entries.

## Verification

### Unit tests

```bash
# PromptSource
./test.py --backend -- src/tests/test_agent/test_loop/test_prompt_source.py -v

# LoopManager
./test.py --backend -- src/tests/test_agent/test_loop/test_manager.py -v

# ScheduleWakeupTool
./test.py --backend -- src/tests/test_tools/test_schedule_wakeup.py -v

# SlashLoopMixin
./test.py --backend -- src/tests/test_agent/test_loop/test_slash.py -v
```

### Integration test

```bash
./test.py --backend -- src/tests/test_agent/test_loop/test_integration.py -v
```

Integration test should verify:

1. `/loop 1m test prompt` creates a loop task.
2. Loop fires after interval and calls `run_synthetic_turn`.
3. `/loop stop` cancels the loop.
4. New `/loop` replaces existing loop.
5. `schedule_wakeup` tool adjusts next interval.
6. Session clear cancels the loop.

### Manual smoke test

```bash
./python.py -m voidx.main
# In the session:
/loop 1m echo hello
# Wait 1 minute — agent should receive "echo hello" as a synthetic turn
/loop status
# Should show: active, interval=60s, next fire in ~30s
/loop stop
# Should show: loop stopped
```
