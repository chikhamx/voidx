# voidx Agent Instructions

## Project Shape
- `src/`: Python backend core — see `src/AGENTS.md`.
- `frontend/`: Web/desktop UI (TypeScript SPA) — see `frontend/AGENTS.md`.
- `desktop/`: Native desktop shell (Tauri 2) — see `desktop/AGENTS.md`.
- `tui/`: Pure terminal TUI (Python) — see `tui/AGENTS.md`.

## Subdirectory AGENTS.md
Each subdirectory has its own `AGENTS.md` for directory-specific details only; global rules live here and are not duplicated. Read both when editing a subdirectory.

## Runtime Environment
- Use `./python.py` as the Python entry point on Linux/macOS/Windows — it locates the voidx venv under `VOIDX_HOME` and forwards all arguments. See `docs/dev-guide.md` for details.

## Commands
- Build wheel: `./python.py scripts/package.py`
- Build + verify wheels (release): `./python.py scripts/package.py --format all --clean --verify`
- Web UI gateway: `./python.py -m voidx.main --web` (open frontend with `?ws=<gateway-url>`)
- Headless web backend: `./python.py -m voidx.main --web --web-headless`
- Export UI protocol schema: `./python.py scripts/export_ui_protocol_schema.py`

## Testing
**Always prefer `./test.py` over invoking pytest/vitest/cargo directly** — it auto-switches to the voidx venv and handles suite selection.

`./test.py` runs three suites: **backend** (pytest over `src/tests` + `tui/tests`), **frontend** (vitest), **desktop** (cargo test).

- Verbose output: `./test.py -v`
- Pass args to the underlying runner with `--`:
  - Backend (pytest): `./test.py --backend -- src/tests/test_foo.py -k "test_bar"`
  - Frontend (vitest): `./test.py --frontend -- --reporter=verbose`
  - Desktop (cargo test): `./test.py --desktop -- --nocapture`

## Code Rules
- Keep modules small and named by responsibility.
- Do not add comments unless they explain non-obvious intent or constraints.
- Don't repeat yourself — single source of truth, no duplicated rules across files.

## Document Rules
- `docs/design/` — exploratory/RFC-stage docs.
- `docs/specs/` — approved designs awaiting or in implementation.
- `docs/archive/` — completed docs. Archive **only after** the final verify step has passed: verify the actual implementation files exist and are functional, then run `./scripts/archive.py docs/specs/<file>.md`.

## Releasing
- Release flow and version file checklist: `docs/releasing.md` (single source of truth — do not duplicate).

## Safety
- Do not commit `.voidx/`, `.env*`, or local credentials.
- Preserve user work in a dirty tree; never revert unrelated changes.
- Run the relevant focused tests before broad test runs.
