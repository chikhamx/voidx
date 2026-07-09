# Subagent Result Preview Design

> **Status: Done**

Date: 2026-06-07

## Problem

When a child agent finishes, its full output text is rendered to the UI as an
expanded `tool_result` node. For long-running explore/review/plan agents, this
can be dozens or hundreds of lines of detailed findings that flood the output.
The orchestrator still needs the full output for synthesis, but the user-facing
UI only needs a bounded preview of the child agent's conclusion.

## Current Flow

```
subagent.py run_subagent()
  → returns full text output
→ AgentTool.execute()
  → ToolResult(output=full_text)
→ tool_execution.py:196  (elif self._debug or tid == "agent")
  → ToolResultAppended(text=full_text, collapsed=False)
→ dock.append_tool_result()
  → creates tool_result node, all lines visible
```

Key code locations:

| File | Lines | Role |
|------|-------|------|
| `src/voidx/agent/graph/subagent.py` | 262-274 | Returns full text from child agent |
| `src/voidx/tools/agent.py` | 96-105 | Wraps output in `ToolResult(title=..., output=full_text)` |
| `src/voidx/agent/graph/tool_execution.py` | 196-209 | Unconditionally emits `ToolResultAppended` for `agent` tool, `collapsed=False` |
| `src/voidx/ui/output/dock/nodes.py` | 167-198 | `append_tool_result()` renders all lines in an expanded node |
| `src/voidx/ui/output/events/__init__.py` | 317-326 | `ToolResultAppended` handler calls `append_tool_result` |
| `src/voidx/ui/output/tree.py` | 395-400 | Renders `Agent(...)` child subagent wrapper nodes transparently |

## Design

### Replace full UI output with a bounded preview

When `tid == "agent"`, keep emitting a `ToolResultAppended` event, but pass a
preview string instead of the full child-agent output. The full text still flows
to the orchestrator via `ToolMessage`. Only the UI-rendered final result block is
bounded.

Do not distinguish debug mode for this behavior. Debug can still show other
tool outputs, but agent final output should stay bounded so long-running child
agents do not flood the terminal.

This mirrors the thinking renderer's bounded-output pattern: show a small
visible slice and add an omitted-content marker when content is longer. Agent
results should use the beginning of the final output because child-agent
reports normally put the conclusion first.

The omitted marker must be plain text, not Rich markup. `append_tool_result()`
escapes result lines before rendering, so strings such as `[dim]...[/dim]`
would be displayed literally.

### Changes

#### 1. `tool_execution.py` — preview `ToolResultAppended` for agent tool

In the `elif self._debug or tid == "agent"` branch, when `tid == "agent"`,
send a preview to UI sinks and keep the full `result.output` for the returned
`ToolMessage`.

```python
# Before (tool_execution.py:196-209):
elif self._debug or tid == "agent":
    if via_events():
        await ui_events.emit(ToolResultAppended(
            tool_call_id=tool_event_id,
            text=result.output,
        ))
    elif tool_node:
        dock.append_tool_result(
            result.output,
            parent=tool_node,
            tool_call_id=tool_event_id,
        )
    else:
        ui.tool_result(result.output)

# After:
elif self._debug or tid == "agent":
    output = _agent_result_preview(result.output) if tid == "agent" else result.output
    if via_events():
        await ui_events.emit(ToolResultAppended(
            tool_call_id=tool_event_id,
            text=output,
        ))
    elif tool_node:
        dock.append_tool_result(
            output,
            parent=tool_node,
            tool_call_id=tool_event_id,
        )
    else:
        ui.tool_result(output)
```

The `ToolMessage` with full output is still created after rendering and returned
to the orchestrator. Only the UI rendering text is shortened.

#### 2. Preview helper

Add a small helper in `tool_execution.py`:

```python
AGENT_RESULT_PREVIEW_LINES = 5
AGENT_RESULT_PREVIEW_CHARS = 1200

def _agent_result_preview(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return text

    lines = stripped.splitlines()
    visible = lines[:AGENT_RESULT_PREVIEW_LINES]
    omitted_lines = max(0, len(lines) - len(visible))

    preview = "\n".join(visible)
    omitted_chars = max(0, len(preview) - AGENT_RESULT_PREVIEW_CHARS)
    if len(preview) > AGENT_RESULT_PREVIEW_CHARS:
        preview = preview[:AGENT_RESULT_PREVIEW_CHARS].rstrip()

    suffixes = []
    if omitted_lines:
        suffixes.append(f"{omitted_lines} more lines")
    if omitted_chars:
        suffixes.append(f"{omitted_chars} more chars")
    if suffixes:
        preview = f"{preview}\n... ({'; '.join(suffixes)} omitted; full result passed to orchestrator)"
    return preview
```

The exact constants can be adjusted after usage, but the helper should cap both
line count and character count. This avoids both long multi-line reports and
single-line floods.

The helper is intentionally local to `tool_execution.py` for V1. This keeps the
contract narrow: only final UI rendering for `agent` tool results changes, while
tool message sanitization and transcript persistence remain on the existing
path.

### What does NOT change

- **Full text still reaches the orchestrator** — `ToolMessage` with `sanitize_tool_message_content(result.output)` is created regardless of UI rendering.
- **Debug mode does not change agent previewing** — child-agent final output is previewed in UI even when debug is enabled.
- **No new summary phase** — do not add `SubagentSummarizing`, a second model call, or a synthetic summary event.
- **No collapse-based hiding** — do not rely on `SubagentFinished.collapsed` or `ToolResultAppended.collapsed` to hide content. The agent tool result should be rendered expanded but bounded.
- **Subagent streaming during execution** — `StreamingRenderer` and `CaptureConsole` still stream step-by-step output during the agent's run. Only the final result block is shortened.
- **`SubagentStarted` / `SubagentFinished` events** — still emitted, still update node headers.
- **Child tool trace** — child agent tool calls remain visible while the child agent runs.
- **Transparent subagent wrapper rendering** — the existing render-only flattening for `Agent(...)` child subagent wrappers remains unchanged.

### Implementation Plan

1. Add `_agent_result_preview()` to `tool_execution.py`.
2. In `tool_execution.py`, use the preview string for UI rendering when `tid == "agent"`.
3. Keep `ToolMessage` construction unchanged so the orchestrator receives full output.
4. Update the existing agent-tool UI test that currently expects full final output.
5. Add focused tests for line truncation, character truncation, and debug-independent previewing.

### Existing Test Migration

`test_agent_tool_suppresses_child_stream_but_keeps_final_result` should keep its
core assertion that the child assistant stream is suppressed, but its final
result assertions should change:

- UI tree contains the bounded preview and omitted marker for long output.
- UI tree does not contain omitted lines from the child-agent final output.
- Returned messages still contain the full `ToolMessage` content and full
  child `AIMessage` content for orchestrator synthesis.

### Testing

| Test | Description |
|------|-------------|
| `test_agent_tool_result_preview_preserves_short_output` | Short child-agent final output renders unchanged in the UI |
| `test_agent_tool_result_previewed_in_ui` | Agent final result renders as a bounded preview in the UI |
| `test_agent_tool_result_preview_omits_extra_lines` | Preview shows the first lines and an omitted-lines marker |
| `test_agent_tool_result_preview_caps_long_single_line` | Preview caps very long single-line child output |
| `test_agent_tool_result_preview_does_not_depend_on_debug` | Agent final output is previewed even when debug mode is enabled |
| `test_subagent_full_output_reaches_orchestrator` | `ToolMessage` still contains full child-agent output |

### Acceptance Criteria

- Long child-agent final outputs no longer flood the terminal after the agent
  tool finishes.
- The main agent receives the same full result text it receives today.
- Debug mode does not create a separate, full-output path for agent results.
- No new summarization model call, collapsed wrapper behavior, or frontend
  protocol shape is required.
