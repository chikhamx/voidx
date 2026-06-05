# Parallel Sub-Agent Execution Design

Date: 2026-06-05

## Goal

Enable the orchestrator to dispatch multiple child agents concurrently instead of sequentially. This allows parallel exploration of different code areas, simultaneous implementation + review, and faster task completion for independent subtasks.

## Current State

Key files:

- `src/voidx/agent/graph/subagent.py` — `run_subagent()` runs a single child agent synchronously within the tool execution loop.
- `src/voidx/tools/agent.py` — `AgentTool` invokes `self._run_child_agent()` which calls `run_subagent()`. One agent at a time.
- `src/voidx/agent/graph/tool_execution.py` — `_execute_tools()` processes tool calls sequentially in a for-loop.
- `src/voidx/tools/task_tracker.py` — `TaskTracker` already has `start()`, `update()`, `finish()` for tracking tasks, but only used for status reporting, not for parallel execution.
- `src/voidx/tools/task_status.py` — `TaskStatusTool` reports on running tasks.
- `src/voidx/agent/agents.py` — depth limit = 1 (child agents cannot start further child agents).

Observed gaps:

- **Sequential only** — if the orchestrator needs to explore 3 modules, it makes 3 sequential `agent` tool calls, each waiting for the previous to complete.
- **No concurrent dispatch** — the `agent` tool returns a single result; there's no way to fire off multiple agents and collect results later.
- **TaskTracker is passive** — it records task state but doesn't drive execution.
- **No result aggregation** — when multiple agents run, there's no mechanism to merge their outputs into a coherent response.
- **Resource unbounded** — no limit on concurrent LLM calls, which could exhaust API rate limits or token budgets.

## External References

- **Claude Code** `Task` tool: supports parallel sub-agent dispatch with result collection.
- **Devin** parallel workers: multiple agents work on independent subtasks simultaneously.
- **OpenAI Codex** sub-agents: parallel execution with depth limits.
- **AutoGen** group chat: multiple agents converse in parallel with message routing.

References:

- https://code.claude.com/docs/en/subagents
- https://developers.openai.com/codex/subagents
- https://microsoft.github.io/autogen/

## Design

### Approach: Parallel Agent Dispatch with asyncio.gather

Add a `parallel` mode to the `agent` tool that accepts multiple task descriptions and runs them concurrently using `asyncio.gather`. Results are collected and merged before returning to the orchestrator.

### Tool Changes

#### Option A: Extend `agent` tool with batch mode

```python
class AgentInput(BaseModel):
    agent: str = Field(
        description="Child agent to run: explore, plan, implement, or review."
    )
    description: str = Field(
        description="Complete, self-contained task description for the child agent."
    )
    model: str | None = Field(default=None)

class AgentBatchInput(BaseModel):
    tasks: list[AgentInput] = Field(
        description="List of agent tasks to run in parallel. Max 4 tasks.",
        max_length=4,
    )
```

#### Option B: New `agent_parallel` tool

```python
class AgentParallelInput(BaseModel):
    tasks: list[AgentInput] = Field(
        description=(
            "List of agent tasks to run in parallel. Each task is independent. "
            "Max 4 tasks. All tasks must use the same agent type or compatible types "
            "(explore, plan, review — not implement)."
        ),
        max_length=4,
    )
```

**Recommendation: Option B** — a separate tool is cleaner because:
1. The LLM sees a distinct tool name and knows when parallelism is appropriate.
2. Permission rules can differ (parallel implement is riskier than parallel explore).
3. The single `agent` tool stays simple and backward-compatible.

### Execution Model

```python
class AgentParallelTool(BaseTool):
    id = "agent_parallel"
    description = (
        "Start multiple child agents in parallel for independent tasks. "
        "Use when you need to explore, review, or plan across multiple areas simultaneously. "
        "Max 4 concurrent tasks. Tasks must be independent — no shared mutable state."
    )

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = AgentParallelInput.model_validate(args)

        # Validate: no implement agents in parallel (too risky)
        for task in inp.tasks:
            if task.agent == "implement":
                return ToolResult(
                    output="Cannot run implement agents in parallel. Use sequential agent calls for code changes.",
                    metadata={"error": True},
                )

        # Create task tracker entries
        task_ids = []
        for task in inp.tasks:
            tracker_id = self._tracker.start(
                task_id=_uid(),
                agent=task.agent,
                description=task.description,
                max_steps=25,
            )
            task_ids.append(tracker_id.id)

        # Run all agents concurrently
        results = await asyncio.gather(
            *[
                self._run_with_tracker(task, tracker_id, ctx)
                for task, tracker_id in zip(inp.tasks, task_ids)
            ],
            return_exceptions=True,
        )

        # Aggregate results
        return self._aggregate_results(inp.tasks, results, task_ids)
```

### Concurrency Control

```python
class AgentConcurrencyLimiter:
    """Limit concurrent agent executions to avoid API rate limits."""

    def __init__(self, max_concurrent: int = 4, max_per_minute: int = 20):
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._rate_limiter = TokenBucketRateLimiter(max_per_minute, period=60)

    async def acquire(self) -> None:
        await self._rate_limiter.wait()
        await self._semaphore.acquire()

    def release(self) -> None:
        self._semaphore.release()

class TokenBucketRateLimiter:
    """Simple token bucket for API rate limiting."""

    def __init__(self, capacity: int, period: float = 60.0):
        self._capacity = capacity
        self._period = period
        self._tokens = capacity
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            self._refill()
            if self._tokens >= 1:
                self._tokens -= 1
                return
            # Wait for next token
            wait_time = self._period / self._capacity
        await asyncio.sleep(wait_time)
```

The concurrency limiter is shared across all agent executions (sequential and parallel).

### Result Aggregation

```python
def _aggregate_results(
    self,
    tasks: list[AgentInput],
    results: list[str | BaseException],
    task_ids: list[str],
) -> ToolResult:
    sections = []
    total_success = 0
    total_failure = 0

    for task, result, tid in zip(tasks, results, task_ids):
        if isinstance(result, BaseException):
            total_failure += 1
            sections.append(
                f"## {task.agent}: {task.description[:60]}\n"
                f"Status: ❌ FAILED\n"
                f"Error: {str(result)[:500]}\n"
            )
        else:
            total_success += 1
            sections.append(
                f"## {task.agent}: {task.description[:60]}\n"
                f"Status: ✅ COMPLETED\n"
                f"{result}\n"
            )

    header = f"Parallel results: {total_success} succeeded, {total_failure} failed"
    return ToolResult(
        title=header,
        output=f"{header}\n\n" + "\n".join(sections),
        metadata={
            "parallel": True,
            "total": len(tasks),
            "succeeded": total_success,
            "failed": total_failure,
            "task_ids": [t.id if hasattr(t, 'id') else t for t in task_ids],
        },
    )
```

### Safety Constraints

1. **No parallel implement** — only `explore`, `plan`, and `review` agents can run in parallel. Implement agents modify files and could conflict.
2. **Max 4 concurrent tasks** — prevents API rate limit exhaustion.
3. **Shared workspace, no shared state** — parallel agents read the same files but must not write. Enforced by:
   - `explore` and `plan` are read-only by design.
   - `review` is read-only by design.
   - If an `implement` agent is requested in parallel, the tool returns an error.
4. **Token budget tracking** — each parallel agent's token usage is tracked and summed into the parent's usage stats.
5. **Timeout per task** — each parallel task has a configurable timeout (default 120s). If a task times out, it's cancelled and reported as failed.

### Task Tracker Integration

The existing `TaskTracker` already supports the needed operations:

```python
# Before starting parallel tasks
for task in tasks:
    tracker.start(task_id, agent, description, max_steps=25)

# During execution (called by run_subagent)
tracker.update(task_id, step=current_step, last_output=preview)

# After completion
tracker.finish(task_id, status="completed" or "error")
```

The `TaskStatusTool` already reports on all running tasks, so users can monitor parallel progress via `/tasks`.

### Streaming and UI

Parallel agents produce interleaved output. Handle this with the existing `OutputTree`:

```python
# Each parallel agent gets its own subtree under the current turn node
for task in tasks:
    agent_node = parent_node.add_child(
        node_type="agent",
        header=f"{task.agent}: {task.description[:60]}",
    )
    # Pass agent_node to run_subagent as capture_tree
```

The TUI and Web Gateway already render the `OutputTree` with collapsible subtrees, so parallel agent output is naturally organized.

### Permission

| Tool | Capability | Default Action |
|------|-----------|---------------|
| `agent_parallel` with explore/plan/review | `AGENT_READONLY` | allow |
| `agent_parallel` with implement | — | deny (tool-level rejection) |

New rule in `BASIC_RULES`:

```python
Rule(permission="agent_parallel", pattern="*", action="allow"),
```

Since implement is blocked at the tool level, the permission rule can be permissive.

### Cancellation

If the user cancels (Ctrl+C) during parallel execution:

```python
async def _run_with_cancellation(self, tasks, task_ids, ctx):
    """Run tasks with cancellation support."""
    running = []
    for task, tid in zip(tasks, task_ids):
        coro = self._run_single(task, tid, ctx)
        running.append(asyncio.create_task(coro))

    try:
        results = await asyncio.gather(*running, return_exceptions=True)
    except asyncio.CancelledError:
        # Cancel all running tasks
        for t in running:
            t.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        raise
    return results
```

## Scope

In scope:

- `agent_parallel` tool with max 4 concurrent tasks.
- `AgentConcurrencyLimiter` with semaphore + rate limiter.
- Result aggregation with per-task status.
- Task tracker integration for monitoring.
- Safety constraints (no parallel implement, timeout, cancellation).
- Permission integration.
- UI integration via OutputTree subtrees.

Out of scope:

- Parallel implement agents (too risky for now — future with file locking).
- Cross-agent communication (agents can't message each other).
- Dynamic task spawning (all tasks declared upfront).
- Priority-based scheduling (all tasks equal priority).
- Persistent task queue (tasks are ephemeral, in-memory only).

## File Changes

| File | Change |
|------|--------|
| `src/voidx/tools/agent_parallel.py` | New — `AgentParallelTool`, `AgentParallelInput`, aggregation logic |
| `src/voidx/tools/concurrency.py` | New — `AgentConcurrencyLimiter`, `TokenBucketRateLimiter` |
| `src/voidx/tools/registry.py` | Register `AgentParallelTool` |
| `src/voidx/agent/graph/tool_execution.py` | Integrate concurrency limiter; handle parallel tool results |
| `src/voidx/agent/graph/subagent.py` | Add timeout support; accept `capture_tree` node for parallel output |
| `src/voidx/permission/engine.py` | Add `agent_parallel` to `BASIC_RULES` |
| `src/voidx/agent/agents.py` | Update prompts to mention `agent_parallel` for independent tasks |
| `tests/test_tools/test_agent_parallel.py` | New — parallel execution, cancellation, rate limiting, aggregation tests |

## Risks

| Risk | Mitigation |
|------|-----------|
| API rate limits hit by concurrent requests | `AgentConcurrencyLimiter` with configurable limits |
| Parallel agents read stale file state | Read-only agents don't conflict; implement is blocked |
| One slow agent blocks result aggregation | Per-task timeout; `asyncio.gather` returns partial results |
| Token budget exhausted by parallel agents | Sum all parallel agents' usage into parent stats; check budget before dispatch |
| Output interleaving confuses users | Each agent gets its own OutputTree subtree |
| Cancellation leaves partial results | Report partial results with "cancelled" status per task |
| LLM overuses parallel when sequential is better | Prompt guidance: "Use agent_parallel only for 2+ independent tasks" |
