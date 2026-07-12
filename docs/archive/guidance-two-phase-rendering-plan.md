# Guidance 两阶段渲染 — Implementation Plan

> **Status: Done** — Archived on 2026-07-11.

## Goal

Implement the two-phase guidance rendering spec: submit-time vibe-line preview, injection-time terminal output as guidance turn nodes.

## Architecture

`submit_guidance()` stops emitting `MessageAppended` and only emits `GuidanceSubmitted` (preview). At LLM call time, `_drain_pending_guidance()` returns `(HumanMessage, truncated)` tuples; `_call_llm()` emits `MessageAppended(style="guidance")` per guidance + one `GuidanceCommitted` (clears preview). Dock consumer routes `GuidanceSubmitted`→preview, `MessageAppended(style="guidance")`→turn node, `GuidanceCommitted`→clear preview.

## Tech Stack

Python, Rich, Pydantic (event schema), pytest.

## File Structure

| File | Responsibility |
|------|---------------|
| `src/voidx/ui/output/events/schema.py` | Add `GuidanceCommitted` event; add to `UiEvent` union |
| `src/voidx/ui/output/events/__init__.py` | Export `GuidanceCommitted` |
| `src/voidx/runtime/ui.py` | Add `GuidanceCommitted = _LazyAttr(...)` |
| `src/voidx/agent/graph/contracts.py` | Change `_pending_guidance: list[str]` → `list[tuple[str, bool]]` |
| `src/voidx/agent/graph/core/voidx_graph.py` | `submit_guidance`: drop `MessageAppended`, store `(text, truncated)`; `_drain_pending_guidance`: return `list[tuple[HumanMessage, bool]]` |
| `src/voidx/agent/graph/tool_executor/guards.py` | Fallback append: `(guidance.message, False)` |
| `src/voidx/agent/graph/core/llm.py` | After drain: emit `MessageAppended(style="guidance")` per guidance + `GuidanceCommitted` |
| `src/voidx/ui/output/events/consumers.py` | `GuidanceSubmitted`→`set_guidance_preview`; `MessageAppended(style="guidance")`→`append_guidance_turn`; `GuidanceCommitted`→`clear_guidance_preview` |
| `src/voidx/ui/output/dock/app.py` | Add `_guidance_preview` field + `set_guidance_preview`/`clear_guidance_preview`/`append_guidance_turn` methods |
| `src/voidx/ui/output/dock/status.py` | Add `active_guidance_preview_text()` |
| `src/voidx/ui/output/dock/__init__.py` | Export `active_guidance_preview_text` |
| `tui/voidx_cli/render_activity.py` | Add `⚡{preview}` to `_busy_activity_label` details |
| `src/voidx/ui/gateway/adapter.py` | Map `GuidanceSubmitted`→preview item; `GuidanceCommitted`→clear item |
| `src/tests/test_agent/test_guide_command.py` | Update assertions: no `MessageAppended`, only `GuidanceSubmitted`; tuple storage |
| `src/tests/test_agent/test_guard_guidance.py` | Update assertions: tuple storage |
| `src/tests/test_ui/gateway/test_ui_events_dock_prompts.py` | Update guidance dock tests |
| `src/tests/test_ui/gateway/test_adapter.py` | Update guidance adapter tests |
| `tui/tests/test_status_activity.py` | Add vibe-line preview test |

## Tasks

### Task 1: Add `GuidanceCommitted` event type
- [ ] 1.1 In `src/voidx/ui/output/events/schema.py`, add class `GuidanceCommitted(UiEventBase)` with `kind: Literal["guidance.committed"] = "guidance.committed"` after `GuidanceSubmitted`
- [ ] 1.2 Add `GuidanceCommitted` to the `UiEvent` union type alias
- [ ] 1.3 In `src/voidx/ui/output/events/__init__.py`, add `GuidanceCommitted` to import list and `__all__`
- [ ] 1.4 In `src/voidx/runtime/ui.py`, add `GuidanceCommitted = _LazyAttr("voidx.ui.output.events", "GuidanceCommitted")` (alphabetical after `GuidanceSubmitted` line 220)
- **Test**: `./test.py --backend -- src/tests/test_ui/gateway/test_adapter.py -k "guidance" -v` (will fail until adapter updated)

### Task 2: Change `_pending_guidance` to tuple storage
- [ ] 2.1 In `src/voidx/agent/graph/contracts.py:188`, change `_pending_guidance: list[str]` → `_pending_guidance: list[tuple[str, bool]]`
- [ ] 2.2 In `src/voidx/agent/graph/core/voidx_graph.py:428`, change `self._pending_guidance.append(guidance)` → `self._pending_guidance.append((guidance, truncated))`
- [ ] 2.3 In `src/voidx/agent/graph/core/voidx_graph.py:435-443`, change `_drain_pending_guidance` to return `list[tuple[HumanMessage, bool]]`: pop `(text, truncated)`, append `(HumanMessage(...), truncated)`
- [ ] 2.4 In `src/voidx/agent/graph/tool_executor/guards.py:151`, change `pending.append(guidance.message)` → `pending.append((guidance.message, False))`
- **Test**: `./test.py --backend -- src/tests/test_agent/test_guide_command.py -v` (will fail until submit_guidance updated)

### Task 3: Remove `MessageAppended` from `submit_guidance`
- [ ] 3.1 In `src/voidx/agent/graph/core/voidx_graph.py:422-427`, delete the `if source == "user" and self._ui.via_events():` block that emits `MessageAppended`
- [ ] 3.2 Restructure: emit `GuidanceSubmitted(text=guidance, truncated=truncated)` only for user when `via_events()`. Guard guidance does not emit `GuidanceSubmitted`, directly appends to queue and returns `True`. For user: if emit fails → return `False` (don't append).
- [ ] 3.3 New logic:
  ```python
  if source == "user" and self._ui.via_events():
      if not self._ui.events.emit_direct(GuidanceSubmitted(text=guidance, truncated=truncated)):
          return False
  self._pending_guidance.append((guidance, truncated, source))
  return True
  ```
- **Test**: `./test.py --backend -- src/tests/test_agent/test_guide_command.py src/tests/test_agent/test_guard_guidance.py -v`

### Task 4: Emit guidance render events in `_call_llm`
- [ ] 4.1 In `src/voidx/agent/graph/core/llm.py`, add imports: `from voidx.runtime.ui import GuidanceCommitted, MessageAppended` (or extend existing import block)
- [ ] 4.2 After `guidance_messages = self._drain_pending_guidance()` (line 211), the return is now `list[tuple[HumanMessage, bool]]`. Unpack: `guidance_pairs = guidance_messages; guidance_messages = [msg for msg, _ in guidance_pairs]`
- [ ] 4.3 After drain, if `self._ui.via_events()` and `guidance_pairs` is non-empty: for each `(msg, truncated)`, emit `MessageAppended(text=display_text, style="guidance")` where `display_text = f"{msg.content} [truncated]" if truncated else msg.content`. After all, emit `GuidanceCommitted()`.
- **Test**: `./test.py --backend -- src/tests/test_agent/test_guide_command.py -v`

### Task 5: Dock consumer routing
- [ ] 5.1 In `src/voidx/ui/output/events/consumers.py:188-189`, change `GuidanceSubmitted()` case to `return self._dock.set_guidance_preview(event.text)`
- [ ] 5.2 Change `MessageAppended(text=text, style=style)` case (line 178-179): if `style == "guidance"`, call `self._dock.append_guidance_turn(text)` instead of `append_message`
- [ ] 5.3 Add `GuidanceCommitted()` case: `return self._dock.clear_guidance_preview()`
- **Test**: `./test.py --backend -- src/tests/test_ui/gateway/test_ui_events_dock_prompts.py -v`

### Task 6: Dock methods
- [ ] 6.1 In `src/voidx/ui/output/dock/app.py` `__init__`, add `self._guidance_preview: str = ""`
- [ ] 6.2 Add `set_guidance_preview(self, text: str)`: store `text` to `self._guidance_preview`, call `self.refresh()`
- [ ] 6.3 Add `clear_guidance_preview(self)`: set `self._guidance_preview = ""`, call `self.refresh()`
- [ ] 6.4 Add `append_guidance_turn(self, text: str)`: create `node_type="turn"` node, header `[bold white]⚡[/] {escaped_header}`, reuse `_render_turn_text()`, `_mark_settled()`, insert before `_stream_node` if exists, call `self.refresh()`
- **Test**: `./test.py --backend -- src/tests/test_ui/gateway/test_ui_events_dock_prompts.py -v`

### Task 7: Status helper + export
- [ ] 7.1 In `src/voidx/ui/output/dock/status.py`, add `active_guidance_preview_text()` function: returns `getattr(get_dock(), "_guidance_preview", "") or ""`
- [ ] 7.2 In `src/voidx/ui/output/dock/__init__.py`, import and export `active_guidance_preview_text`
- **Test**: `./test.py --backend -- tui/tests/test_status_activity.py -v`

### Task 8: TUI vibe-line preview
- [ ] 8.1 In `tui/voidx_cli/render_activity.py`, import `active_guidance_preview_text` from `voidx.ui.output.dock`
- [ ] 8.2 In `_busy_activity_label()`, after `latest = self._latest_action_text()` block (around line 108), add: `preview = active_guidance_preview_text()`; if preview: `details.append(f"⚡{_clip_cells(preview, 40)}")`
- **Test**: `./test.py --backend -- tui/tests/test_status_activity.py -v`

### Task 9: Gateway adapter mapping
- [ ] 9.1 In `src/voidx/ui/gateway/adapter.py`, add `_on_guidance_submitted` handler: return `item.started` with `kind="guidance_preview"`, `data={"text": event.text, "truncated": event.truncated}`
- [ ] 9.2 Add `_on_guidance_committed` handler: return `item.started` with `kind="guidance_preview"`, `lifecycle="completed"` (or a clear notification)
- [ ] 9.3 Add `GuidanceSubmitted` and `GuidanceCommitted` to `_HANDLERS` dict
- **Test**: `./test.py --backend -- src/tests/test_ui/gateway/test_adapter.py -v`

### Task 10: Update tests
- [ ] 10.1 `test_guide_command.py`: update assertions — no `MessageAppended` in emitted; `_pending_guidance` stores tuples; emit failure for user returns `False` with empty queue; guard guidance does not emit `GuidanceSubmitted`, directly appends to queue and returns `True`
- [ ] 10.2 `test_guard_guidance.py`: update `_pending_guidance` assertion to `[("retry differently", False)]`
- [ ] 10.3 `test_ui_events_dock_prompts.py`: update `test_guidance_submitted_event_does_not_render_message` → now sets preview; update `test_message_appended_guidance_renders_in_dock` → now creates turn node with `⚡`
- [ ] 10.4 `test_adapter.py`: update `test_guidance_submitted_is_not_rendered_as_message_item` → now returns a `guidance_preview` item; add `GuidanceCommitted` test
- [ ] 10.5 `test_status_activity.py`: add test that vibe label includes `⚡{preview}` when guidance preview is set
- **Test**: `./test.py --backend -- src/tests/test_agent/test_guide_command.py src/tests/test_agent/test_guard_guidance.py src/tests/test_ui/gateway/test_ui_events_dock_prompts.py src/tests/test_ui/gateway/test_adapter.py tui/tests/test_status_activity.py -v`

### Task 11: Regression
- [ ] 11.1 `./test.py --backend -- src/tests/test_agent/slash/test_slash_session.py -v`
- [ ] 11.2 `./test.py --backend -- src/tests/test_agent/graph/test_run_loop_startup.py -v`
- [ ] 11.3 `./test.py --backend` (full backend)
- [ ] 11.4 `./test.py --backend -- tui/tests/` (full TUI)

## Risks

- **`_drain_pending_guidance` return type change** ripples to `rebuild_llm_messages` which uses `guidance_messages` in `[*messages, *guidance_messages]`. Must unpack to `list[HumanMessage]` before that usage. The `guidance_messages` variable is used at line 235.
- **`MessageAppended(style="guidance")` semantic change** in consumer: must branch on `style == "guidance"` before the generic `append_message` path. Other styles (warning, markdown, diff) must still go through `append_message`.
- **Gateway adapter** `GuidanceSubmitted` was previously unmapped (returned `None`). Now it returns an item — frontends need to handle `guidance_preview` kind, but frontend adaptation is explicitly out of scope per spec.
- **`turn_runner.py` finally block** clears `_pending_guidance` — now contains tuples. The `if pending_guidance:` truthiness check still works on non-empty list of tuples. No change needed.
- **Multiple guidance in one turn**: `set_guidance_preview` overwrites (last wins); `append_guidance_turn` creates one node per guidance; `GuidanceCommitted` clears preview after all nodes created.
