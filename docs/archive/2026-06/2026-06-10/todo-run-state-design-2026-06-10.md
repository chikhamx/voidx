# Todo Run State Design

> **Status: Done**

## Goal

Move todo progress from ordinary tool-result conversation history into runtime task state.

The todo tool should remain available to the model as a progress-reporting tool, and the UI should keep showing the pinned Todo panel. However, todo updates should not pollute the model context as regular `ToolMessage` content, and should not be persisted as normal tool-result chat history.

## Current Behavior

`TodoWriteTool` validates the submitted todo list, mirrors it into `TaskTracker`, and returns a `ToolResult` with formatted output plus `todo_items` / `todo_summary` metadata.

`GraphToolExecutionMixin._execute_tools()` treats todo like any other tool:

1. emit `ToolStarted`;
2. execute the tool;
3. emit `TodoUpdated` when metadata is valid;
4. emit `ToolFinished`;
5. return a `ToolMessage` containing the formatted todo output.

Turn persistence then saves that `ToolMessage` as a normal tool row. Future LLM calls can see the todo output as ordinary tool context.

The recent UI-only change hides the visible `Todo(...)` tool node, but it does not change the semantic message history. Todo still enters graph state and context as a tool result.

## Problem

Todo is state, not evidence. Treating it as a normal `ToolMessage` has three downsides:

- It adds low-value repeated progress text to the LLM context.
- It can make the model overfit to its own plan instead of actual tool evidence.
- It visually looks fixed after hiding the UI node, while semantically it still occupies tool-result history.

There is also a protocol constraint: we cannot simply stop returning a todo `ToolMessage` while leaving the preceding assistant `tool_call` intact. OpenAI/Anthropic-style tool-call protocols require each assistant tool call to be paired with a corresponding tool result. The existing replay repair path may also synthesize a placeholder result for missing tool outputs. Therefore, todo must be removed from LLM replay as a pair: both the todo tool call and the todo tool result.

## Non-Goals

- Do not change the todo tool input schema in this phase.
- Do not add hard enforcement that the model must follow every todo item before acting.
- Do not remove the pinned Todo UI.
- Do not remove final Todo transcript snapshots; users still need visible progress history.
- Do not change non-todo tool-call adjacency or persistence.

## Chosen Direction

Introduce first-class `TodoRunState` in the runtime task layer, and treat the todo tool as a state update.

Todo state becomes available through structured runtime state, while todo tool call/result messages are sanitized out of model replay and normal persistence.

## Data Model

Add Pydantic models near the runtime task state layer:

```python
class TodoRunItem(BaseModel):
    content: str
    status: Literal["pending", "in_progress", "completed", "cancelled"]


class TodoRunState(BaseModel):
    summary: str = ""
    items: list[TodoRunItem] = Field(default_factory=list)
    updated_at: str = ""
```

Add the current todo state to `TaskState` rather than only `TaskRun`.

Reasoning:

- todo is useful in auto, plan, and goal modes;
- `TaskRun` can be idle outside goal mode;
- `TaskState` already persists session-level runtime context across turns.

`TaskTracker` may continue mirroring todos for the existing `task_status` tool, but it should not be the canonical persisted source.

## Execution Flow

When a todo tool finishes successfully:

1. validate `todo_items` / `todo_summary` from tool metadata;
2. build a `TodoRunState`;
3. update graph state with `todo_state` (`AgentState.todo_state`);
4. update `self._task_state.todo_state`;
5. mirror to `TaskTracker`;
6. emit `TodoUpdated` for UI;
7. do not return a normal todo `ToolMessage`.

The validation/build step should be a single helper shared by tool execution,
UI event generation, and subagent execution. Silent divergence between
`TodoUpdated` payload parsing and runtime state parsing would make the pinned UI
and the runtime context disagree.

The live LangGraph message reducer may still contain the raw assistant message
that originally requested the todo tool. That is acceptable only as internal
ephemeral graph state. Any boundary that sends messages to an LLM, saves
messages, snapshots a context frame, or compacts semantic history must use the
sanitized replay form described below.

When a todo tool fails or returns malformed metadata:

- do not update `TodoRunState`;
- surface a warning/error event;
- avoid adding formatted todo output as ordinary context.

Non-todo tools continue to return normal `ToolMessage` objects.

## Context Sanitization

Before sending messages to the LLM, sanitize todo from semantic replay:

1. scan assistant messages for tool calls whose `name == "todo"`;
2. record their tool call ids;
3. remove those todo calls from `AIMessage.tool_calls`;
4. remove corresponding content blocks if providers encode tool calls in structured content (`tool_use`, DSML invoke blocks, etc.);
5. skip `ToolMessage` rows whose `tool_call_id` belongs to a removed todo call;
6. if an assistant message has no visible content and no remaining tool calls, drop it.

Mixed tool batches must be preserved. For example, if one assistant message calls `todo` and `read`, only the todo call and its todo result are removed; the `read` call and result stay adjacent.

This sanitizer should run before any missing-tool-result repair, so replay does not synthesize a placeholder for removed todo calls.

Implementation placement:

- `ContextCompiler` can strip legacy persisted rows while assembling semantic
  history, but it is not sufficient by itself because the graph edge
  `execute_tools -> call_llm` does not run through `prepare` again.
- `_call_llm` should sanitize the `llm_messages` it uses for token estimates,
  context-frame snapshots, compaction inputs, and model invocation.
- `_stream_llm` should keep a defensive sanitizer before repair, because tests
  and future callers may invoke it directly.
- Turn persistence should sanitize newly produced assistant/tool rows before
  saving them. Otherwise a persisted assistant row can keep a todo tool call
  without a corresponding tool row, and replay repair will synthesize the exact
  placeholder this design is meant to avoid.

The sanitizer should be deterministic and idempotent, so it is safe to run at
multiple boundaries.

## Runtime Context Rendering

The LLM should receive todo progress as structured runtime state, not as tool output.

Add a concise section to the runtime task-state overlay when todo state exists:

```text
## Current Todo
0/3 done · 1 active · 2 pending
- in_progress: inspect stream handling
- pending: write regression test
- pending: verify focused tests
```

This is intentionally separate from `ToolMessage` history. It gives the model current progress without preserving every todo tool invocation.

Render this section only when `TaskState.todo_state` exists and has at least one
item. A successful empty todo list clears runtime todo state immediately; it
does not render a persistent `0/0` section.

## Persistence

Add `todo_state_json` to persisted session runtime state.

Save/load behavior:

- `save_runtime_state()` persists `TaskState.todo_state`;
- `load_runtime_state()` restores it;
- `/clear` and session reset clear it;
- resumed sessions can render pinned Todo from restored runtime state if desired.

Normal message persistence must save the sanitized replay form:

- skip todo `ToolMessage` rows once execution no longer returns them;
- remove todo calls from assistant rows before saving new rows;
- preserve non-todo calls/results in mixed tool batches with their original
  adjacency.

Legacy sessions may still contain old todo tool rows; context sanitization
should tolerate and strip them when the preceding assistant todo call id can be
identified.

Transcript persistence remains separate:

- pinned Todo is live runtime state;
- `TodoCommitted` can still append a transcript todo node at turn end for user-visible history;
- transcript todo nodes are not replayed as LLM tool context.

## Subagents

Subagent todo updates should follow the same model:

- child todo tool results update run-level todo state through structured metadata;
- UI events can include `agent_id` as today;
- child todo tool output should not be appended to the child message buffer as ordinary `ToolMessage` context.
- parent orchestration should pass a small todo-state callback/sink into
  `run_subagent()` so child todo updates can update the same session-level
  runtime state as top-level todo calls.

If child agents need private todo state later, that should be a separate design. This phase keeps a single session-level todo state, matching the current global pinned Todo display.

## Adherence

This design does not force the model to follow todo order. It makes todo state explicit and cleanly available, which enables later enforcement.

Possible later gates:

- warn when a turn finishes with stale `in_progress` todos;
- require a todo update before final answer when pending/in-progress items remain;
- compare non-todo tool activity against the active todo item and inject guidance on obvious drift.

Those gates should be designed separately because they affect autonomy and recovery behavior.

## Test Plan

Focused tests:

- todo execution updates `AgentState.todo_state` / `TaskState.todo_state`;
- todo execution emits `TodoUpdated` but returns no normal todo `ToolMessage`;
- mixed tool batch preserves non-todo `ToolMessage` adjacency;
- replay sanitizer removes todo tool calls and todo tool messages as a pair;
- replay sanitizer runs before missing-tool-result repair;
- turn persistence does not save new todo `ToolMessage` rows;
- runtime state persistence saves and restores `todo_state_json`;
- pinned Todo UI still updates and commits transcript todo nodes;
- subagent todo updates do not leak todo output into child message buffers.

Regression tests:

- existing non-todo tool execution and persistence remain unchanged;
- legacy sessions with old todo `ToolMessage` rows do not produce protocol repair placeholders;
- malformed todo metadata does not overwrite valid prior todo state.

## Open Questions

- Resolved: show todo state to the model whenever `TaskState.todo_state` has
  items, including all-completed lists, because the model still benefits from
  knowing current progress.
- Resolved: a successful empty todo list clears runtime todo state immediately.
- Resolved: malformed todo metadata should create a lightweight warning and
  leave the prior valid todo state unchanged.
- Deferred: do not restore runtime state from final transcript todo nodes in
  this phase. Transcript nodes are user-visible history, not canonical runtime
  state.
