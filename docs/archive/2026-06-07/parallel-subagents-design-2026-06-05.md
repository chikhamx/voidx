# Parallel Sub-Agent Execution Design

> **Status: Done**

Date: 2026-06-05
Updated: 2026-06-07

## Goal

Give voidx an opt-in ability for the orchestrator to split independent work
across multiple child agents in the same turn and let those child agents run
concurrently. This supports workflows such as parallel exploration, parallel
review, parallel implementation of isolated areas, and mixed exploration plus
review when the subtasks do not depend on each other.

The feature must be hidden and disabled by default. When the switch is off,
the model must not be told that child agents can run concurrently, no
parallel-agent tool should be visible, and the executor must not run multiple
`agent` calls concurrently even if the model emits them accidentally.

## Current State

Key files:

- `src/voidx/agent/graph/tool_execution.py` - `_execute_tools()` already runs
  approved non-barrier tool calls through `asyncio.gather`.
- `src/voidx/tools/agent.py` - `AgentTool` starts one child agent per tool
  call. Multiple `agent` tool calls can appear in one assistant response.
- `src/voidx/agent/graph/subagent.py` - `run_subagent()` runs a single child
  agent. Child agents cannot start nested child agents because their tool
  registry filters out `agent` and `task_status`.
- `src/voidx/agent/agents.py` - the base/orchestrator/implement prompts
  currently state unconditionally that multiple tool calls in one response run
  in parallel.
- `src/voidx/agent/graph/core.py` - filters visible tools per agent role and
  current state before calling the model.
- `src/voidx/agent/graph/wiring.py` - registers the `agent` tool with child
  agent descriptions.
- `src/voidx/config/models.py` and `src/voidx/config/settings_agent.py` -
  hold runtime config and agent-specific settings.

Observed gaps:

- **Parallel subagents are not gated** - the executor currently treats `agent`
  like any other approved non-barrier tool call, so multiple child agents can
  run concurrently with no feature switch.
- **The prompt leaks the capability** - current prompts teach the model that
  multiple tool calls in one response run in parallel. With `agent` visible,
  this implicitly exposes parallel child-agent execution.
- **No subagent concurrency limit** - there is no max concurrency specifically
  for child agents. A model could emit many `agent` calls in one response.
- **No explicit product mode** - there is no clear distinction between the
  default sequential child-agent behavior and an opt-in parallel-subagent mode.
- **UI has per-agent events but no aggregate status** - subagents already emit
  started/finished events, but there is no explicit "running N agents" status.

## Non-Goals

- Do not add `agent_parallel` in V1.
- Do not change the child-agent depth limit.
- Do not make child agents able to communicate with each other.
- Do not make TaskTracker drive scheduling in V1.
- Do not infer dependency graphs between tasks. The model is responsible for
  only batching independent child-agent tasks when the feature is enabled.

## External References

- Claude Code `Task` tool: supports parallel sub-agent dispatch with result
  collection.
- Devin parallel workers: multiple agents work on independent subtasks.
- OpenAI Codex sub-agents: parallel execution with depth limits.
- AutoGen group chat: multiple agents converse with message routing.

References:

- https://code.claude.com/docs/en/subagents
- https://developers.openai.com/codex/subagents
- https://microsoft.github.io/autogen/

## Design

### Recommendation: Gate Existing `agent` Tool Calls

Use the existing single-task `agent` tool as the public API. When parallel
subagents are enabled, the orchestrator can emit multiple independent `agent`
tool calls in the same assistant response. The executor runs those child
agents concurrently up to the configured limit and returns their results to
the orchestrator in the original tool-call order.

Do not introduce `agent_parallel` for V1. A separate batch tool would duplicate
the existing multi-tool-call mechanism, add a second permission path, and make
default-off visibility harder to reason about. If a future version adds
`agent_parallel`, it must be conditionally registered only when
`parallel_subagents_enabled` is true.

### Configuration

Add explicit runtime config:

```python
class ParallelSubagentsConfig(BaseModel):
    enabled: bool = False
    max_concurrent: int = Field(default=4, ge=1, le=8)
```

Expose it from `Config`, for example:

```python
class Config(BaseModel):
    ...
    parallel_subagents: ParallelSubagentsConfig = Field(
        default_factory=ParallelSubagentsConfig
    )
```

Default behavior:

- `parallel_subagents.enabled = false`
- `parallel_subagents.max_concurrent = 4`

Settings should persist this under a clear config key such as:

```toml
[parallel_subagents]
enabled = false
max_concurrent = 4
```

### Visibility Gating

The feature has three gates. All three are required.

#### 1. Prompt gating

When `parallel_subagents.enabled` is false:

- Do not include any prompt text saying child agents can run in parallel.
- Do not tell the model to issue multiple `agent` tool calls in one response.
- Add a sequential child-agent rule for the orchestrator:
  "Delegate at most one child agent in a response. Wait for the result before
  deciding whether another child agent is needed."
- Generic tool batching guidance must be worded so it does not imply child
  agents are included. For example, prefer "batch independent read/search
  tools" over "multiple tool calls run in parallel."

When `parallel_subagents.enabled` is true:

- Inject an explicit section for the orchestrator:
  "For independent child-agent tasks, you may issue multiple `agent` tool
  calls in one response. They will run concurrently up to the configured
  limit. Each child-agent brief must be complete and self-contained."
- Keep dependent work sequential: wait for one child result before delegating
  follow-up work that depends on it.

Implementation note: this should be generated dynamically during context
construction rather than baked unconditionally into static prompt constants.

#### 2. Tool exposure gating

V1 keeps only the existing `agent` tool.

If `agent_parallel` is ever added later:

- Register it only when `parallel_subagents.enabled` is true.
- Exclude it from tool definitions when the feature is disabled.
- Exclude it from agent allowlists unless the runtime feature flag is enabled.
- Do not mention it in prompts or tool descriptions when disabled.

The existing `agent` tool description may be dynamic:

- Disabled: describe delegated single-child-agent work only.
- Enabled: add a short note that independent child-agent work can be split by
  issuing multiple `agent` calls in the same response.

#### 3. Executor gating

`_execute_tools()` must enforce the switch even if the model emits multiple
`agent` calls unexpectedly.

Use a child-agent semaphore:

```python
agent_limit = (
    config.parallel_subagents.max_concurrent
    if config.parallel_subagents.enabled
    else 1
)
agent_semaphore = asyncio.Semaphore(agent_limit)

async def execute_one_limited(tc):
    if tc["name"] == "agent":
        async with agent_semaphore:
            return await execute_one(tc)
    return await execute_one(tc)
```

When disabled, multiple `agent` calls are serialized. When enabled, only
`agent` calls are limited by `max_concurrent`. Non-agent tool concurrency can
continue using the existing execution model.

Barrier tools keep their existing behavior: if a barrier tool such as
`on_intent`, `clarify`, or `plan_checkpoint` is present, it runs first and
other tool calls are deferred.

### Permission Model

Parallel subagents do not bypass existing permission checks.

- `agent(explore)`, `agent(plan)`, and `agent(review)` keep their existing
  read-only authorization behavior.
- `agent(implement)` keeps its existing write-capable authorization behavior.
- Plan mode still blocks `agent(implement)`.
- Approval is evaluated before execution, exactly as it is for current tool
  calls.

V1 does not ban parallel `implement` outright because one of the target use
cases is parallel implementation of independent areas. The safety controls are:

- disabled by default;
- explicit opt-in prompt exposure;
- existing permission checks;
- max concurrent child agents;
- complete, self-contained child-agent briefs.

Future work can add stricter implement policies if needed, such as:

- allow at most one `implement` child agent per batch;
- require disjoint declared file scopes;
- run implement children in isolated worktrees and merge afterward.

### Result Handling

The executor must preserve provider-safe message ordering:

1. Parent `ToolMessage`s are returned in the original tool-call order.
2. Child-agent buffered messages are appended after the parent tool results,
   grouped by the parent `tool_call_id`.
3. Failed child agents produce a normal `ToolMessage` with error metadata and
   do not cancel sibling child agents.

The orchestrator is responsible for reading all child results and producing
the final synthesis for the user. Do not merge multiple child results into one
synthetic `agent_parallel` result in V1.

### UI Integration

Keep the existing per-subagent `SubagentStarted` and `SubagentFinished` events.
Add aggregate status only when parallel subagents are enabled and more than one
child agent is running in the same tool batch.

Expected UI behavior:

1. Each child agent still has its own node/status.
2. The parent turn may show an aggregate status such as "Running 3 child agents".
3. Results can finish in real time, but the messages sent back to the model
   remain ordered by original tool-call order.
4. When the feature is disabled, no "parallel agents" language appears in UI
   status for serialized agent calls.

### Implementation Plan

1. Add `ParallelSubagentsConfig` to config models and settings loading.
2. Remove unconditional child-agent parallelism wording from static prompts.
3. Add dynamic prompt text based on `parallel_subagents.enabled`.
4. Make the `agent` tool description optionally include parallel-subagent
   guidance only when enabled.
5. Add executor-level semaphore gating for `agent` tool calls.
6. Preserve barrier tool behavior and existing parent/child message ordering.
7. Add optional aggregate UI status for enabled parallel batches.
8. Add tests for default-off visibility and execution behavior.

## Testing

| Test | Description |
|------|-------------|
| `test_parallel_subagents_disabled_prompt_hides_capability` | Prompt does not mention parallel child agents when disabled |
| `test_parallel_subagents_enabled_prompt_exposes_capability` | Prompt tells orchestrator it may issue multiple independent `agent` calls when enabled |
| `test_agent_tool_description_hides_parallel_when_disabled` | `agent` tool description does not mention parallel child agents when disabled |
| `test_agent_tool_description_exposes_parallel_when_enabled` | `agent` tool description exposes parallel child-agent guidance when enabled |
| `test_agent_parallel_tool_not_registered_when_disabled` | Future-proof check: no `agent_parallel` tool is visible by default |
| `test_parallel_subagents_settings_round_trip` | Persistent settings load and save the parallel-subagent config |
| `test_parallel_subagents_disabled_serializes_agent_calls` | Multiple `agent` calls in one response run one at a time when disabled |
| `test_parallel_subagents_enabled_runs_agent_calls_concurrently` | Multiple `agent` calls run concurrently when enabled |
| `test_parallel_subagents_respects_max_concurrent` | More agent calls than `max_concurrent` are throttled |
| `test_parallel_subagents_preserves_tool_message_order` | Tool results remain in original tool-call order despite completion order |
| `test_parallel_subagents_failure_isolated` | One child-agent failure does not cancel siblings |
| `test_parallel_subagents_keeps_barrier_deferral` | Barrier tools still defer non-barrier calls, including `agent` |
| `test_parallel_subagents_plan_mode_blocks_implement` | Existing plan-mode implement restriction still applies |
| `test_parallel_subagents_aggregate_ui_status_enabled_only` | Aggregate UI status appears only for enabled parallel batches |
| `test_execute_tools_keeps_parallel_child_agent_buffers_isolated` | Child-agent buffered messages remain grouped by parent tool call |
