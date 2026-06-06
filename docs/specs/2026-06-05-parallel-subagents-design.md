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
async def run_parallel_agents(tasks: list[AgentInput], ...) -> str:
    """Run multiple agents concurrently and merge results."""
    coros = [
        run_subagent(agent=t.agent, description=t.description, model_override=t.model, ...)
        for t in tasks
    ]
    results = await asyncio.gather(*coros, return_exceptions=True)

    merged = []
    for task, result in zip(tasks, results):
        if isinstance(result, Exception):
            merged.append(f"## {task.agent}: FAILED\n{result}")
        else:
            merged.append(f"## {task.agent}\n{result}")

    return "\n\n---\n\n".join(merged)
```

### Concurrency Limits

- Max 4 concurrent agents (hard limit in `AgentParallelInput.max_length`).
- Each agent still respects the depth limit (depth=1, no nested sub-agents).
- Total token budget is shared across all parallel agents.

### Permission Model

| Agent Type | Parallel Allowed | Permission |
|-----------|-----------------|------------|
| explore | Yes | Allow |
| plan | Yes | Allow |
| review | Yes | Allow |
| implement | No (sequential only) | Ask |

Rationale: parallel `implement` agents could conflict on the same files. Restrict to read-only agents for parallel mode.

### UI Integration

Parallel agents should show concurrent status in the UI:

1. Each agent gets its own output node under the `agent` tool call.
2. Status updates show "Running 3 agents in parallel...".
3. Results stream in as each agent completes.

### Testing

| Test | Description |
|------|-------------|
| `test_parallel_agents_run_concurrently` | Multiple agents run at the same time |
| `test_parallel_agents_merge_results` | Results from all agents are combined |
| `test_parallel_agents_handle_failure` | One agent failing doesn't block others |
| `test_parallel_agents_max_4` | More than 4 tasks is rejected |
| `test_parallel_implement_blocked` | implement agent type is not allowed in parallel mode |
