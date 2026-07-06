# voidx TUI Agent Instructions

## Project Shape
- `voidx_cli/app.py`: `PureTui` — main TUI application class, orchestrates input, rendering, and clipboard.
- `voidx_cli/parser.py`: Input parser — key sequence decoding, paste detection, bracketed-paste handling.
- `voidx_cli/input.py`: Input state machine — cursor, edit, history, completion.
- `voidx_cli/state.py`: `InputState` / `RenderState` — mutable state containers for input and rendering.
- `voidx_cli/render_frame.py`: Frame renderer — absolute-positioning full-frame redraw.
- `voidx_cli/render_input.py`, `render_status.py`, `render_activity.py`, `render_todo.py`: Per-panel renderers.
- `voidx_cli/panels.py`: Panel layout — status bar, activity feed, input dock.
- `voidx_cli/overlays.py`: Overlay rendering — choice menus, confirmations.
- `voidx_cli/terminal_mixin.py`: Terminal mode management — raw mode, alternate screen, resize.
- `voidx_cli/clipboard_mixin.py`, `choice_mixin.py`, `text_prompt_mixin.py`: Behavior mixins composed into `PureTui`.
- `voidx_cli/helpers.py`: Shared constants and helpers (ANSI sequences, row counting).
- `voidx_cli/activity.py`: Activity feed data model.
- `voidx_cli/renderer.py`: Renderer interface.
- `tests/`: pytest coverage — one file per concern, mirroring `voidx_cli/` modules.

## Runtime Environment
- Python entry point: `./python.sh` (Unix) or `.\python.ps1` (Windows) from the repo root.
- The TUI depends on the main `voidx` package — tests use `pythonpath = [".", "../src"]` to resolve both.

## Commands
- Full tests: `./python.sh -m pytest tui/tests/ -v`
- Focused tests: `./python.sh -m pytest tui/tests/test_frame_rendering.py -v`
- Build wheel: `cd tui && ../python.sh -m build --wheel`

## Code Rules
- `PureTui` composes behavior via mixins — keep new behavior in a mixin or dedicated module, not in `app.py`.
- Renderers receive `RenderState` and write to a `Console`; they do not mutate state.
- Input parsing is pure where possible — side effects (clipboard, submit) stay in `app.py`.
- Use `voidx.ui.output.dock` for dock-aware output; never write to stdout directly.
- Do not add comments unless they explain non-obvious intent or constraints.

## Testing
- Tests in `tests/`, one file per concern, mirroring `voidx_cli/` modules.
- `tui_helpers.py` provides shared fixtures (`_tui`, `_plain`, `_rich_plain`).
- `conftest.py` adds the tests directory to `sys.path` so `from tui_helpers import *` works.
- Paste-detection tests use deterministic timing via `monkeypatch` on `time.monotonic`.

## Safety
- Do not commit `dist/`, `build/`, or `*.egg-info/` (build artifacts).
- Preserve user work in a dirty tree; never revert unrelated changes.
