# UI Frontends P0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prepare voidx for TUI, Web, and desktop frontends by fixing current TUI interaction bugs and adding a small frontend/request protocol scaffold.

**Architecture:** Keep the existing prompt_toolkit TUI as the first `UiFrontend` implementation, then add protocol DTOs that can later be transported over WebSocket. This phase is deliberately narrow: preserve runtime behavior while moving request/response vocabulary into typed Pydantic models and fixing deterministic TUI bugs.

**Tech Stack:** Python 3.11, Pydantic v2, prompt_toolkit, pytest.

---

## File Structure

- Modify `tests/test_prompt_tui.py`: add regression tests for transcript logical click mapping, visible body selection rendering, system clipboard paste fallback, and input mouse column handling.
- Modify `src/voidx/ui/app.py`: fix input mouse coordinate handling, add system clipboard paste fallback, and keep body mouse handling efficient.
- Modify `src/voidx/ui/app_components/rendering.py`: use logical body line mappings for click toggles and pass transcript selection ranges into formatted rendering.
- Modify `src/voidx/ui/app_components/formatting.py`: add optional selection-aware formatted text rendering.
- Create `src/voidx/ui/frontend.py`: define frontend/controller protocols and typed request result surface.
- Create `src/voidx/ui/protocol/__init__.py` and `src/voidx/ui/protocol/requests.py`: define Pydantic `UiRequest`/`UiResponse` DTOs for future in-process and WebSocket transports.
- Add focused tests for the protocol DTOs if the public API is not exercised by existing tests.

## Task 1: TUI Mouse And Clipboard Regression Tests

**Files:**
- Modify: `tests/test_prompt_tui.py`

- [ ] **Step 1: Write failing tests**

Add tests that assert:
- Transcript click toggles use prompt_toolkit logical line indexes, even when earlier lines wrap visually.
- Dragging over body text produces selection-highlight fragments, not only hidden internal state.
- Text paste fallback reads macOS system text through `pbpaste` when image paste fails.
- Input mouse positions are already source-relative and must not subtract the prompt width a second time.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_prompt_tui.py -q`

Expected: Failures tied to existing implementation behavior, not syntax/import errors.

## Task 2: Minimal TUI Fixes

**Files:**
- Modify: `src/voidx/ui/app.py`
- Modify: `src/voidx/ui/app_components/rendering.py`
- Modify: `src/voidx/ui/app_components/formatting.py`
- Modify: `tests/test_prompt_tui.py` only if existing tests codify incorrect behavior and need expectations updated.

- [ ] **Step 1: Implement minimal code**

Implement:
- `_toggle_body_node_at(row)` should read `self._visible_body_node_ids[row]`.
- `_render_body()` should no longer depend on visual-row click maps for toggles; if selection highlighting needs visual wrapping later, keep it separate from click targeting.
- `_lines_to_formatted_text(..., selection=...)` should apply `class:selection` to selected plain-text fragments.
- `_handle_input_mouse()` should treat `mouse_event.position.x` as source-relative.
- `Ctrl+V` fallback should use system text (`pbpaste` on macOS) before falling back to prompt_toolkit's in-memory clipboard.
- Body `MOUSE_MOVE` without an active drag should return `NotImplemented`.

- [ ] **Step 2: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_prompt_tui.py -q`

Expected: All prompt TUI tests pass.

## Task 3: Frontend Protocol Scaffold

**Files:**
- Create: `src/voidx/ui/frontend.py`
- Create: `src/voidx/ui/protocol/__init__.py`
- Create: `src/voidx/ui/protocol/requests.py`
- Add/modify tests as needed.

- [ ] **Step 1: Write failing tests**

Add tests that assert:
- A `UiChoiceRequest`, `UiTextRequest`, and `UiPermissionRequest` serialize with stable `kind` and `request_id`.
- A `UiResponse` round-trips through Pydantic validation.
- `UiFrontend`/`UiController` protocols expose the small surface needed by current `PromptToolkitTui` without importing prompt_toolkit.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_ui_events.py -q`

Expected: Import failures for the new protocol module.

- [ ] **Step 3: Implement minimal protocol scaffold**

Keep this phase as scaffolding only. Do not rewire `run_loop.py` until the TUI fixes are green.

- [ ] **Step 4: Run focused tests**

Run:
- `.venv/bin/python -m pytest tests/test_prompt_tui.py -q`
- `.venv/bin/python -m pytest tests/test_ui_events.py -q`

## Task 4: Verification

**Files:**
- All edited files.

- [ ] **Step 1: Read lints for edited files**

Use IDE diagnostics for touched files and fix introduced issues.

- [ ] **Step 2: Run focused test suite**

Run: `.venv/bin/python -m pytest tests/test_prompt_tui.py tests/test_ui_events.py -q`

Expected: All tests pass.
