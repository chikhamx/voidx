# voidx Agent Instructions

## Project Shape
- `src/`: Python backend core — see `src/AGENTS.md`.
- `frontend/`: Web/desktop UI (TypeScript SPA) — see `frontend/AGENTS.md`.
- `desktop/`: Native desktop shell (Tauri 2) — see `desktop/AGENTS.md`.
- `tui/`: Pure terminal TUI (Python) — see `tui/AGENTS.md`.

## Subdirectory AGENTS.md
Each subdirectory has its own `AGENTS.md` for directory-specific details only; global rules live here and are not duplicated. Read both when editing a subdirectory.

## Runtime Environment
- Use `./python.sh` (Unix) or `.\python.ps1` (Windows) as the Python entry point — these locate the voidx venv under `VOIDX_HOME` and forward all arguments. See `docs/dev-guide.md` for details. Commands below use the Unix form; Windows users substitute `.\python.ps1`.

## Commands
- Build wheel: `./python.sh scripts/package.py`
- Build + verify wheels (release): `./python.sh scripts/package.py --format all --clean --verify`
- Web UI gateway: `./python.sh -m voidx.main --web` (open frontend with `?ws=<gateway-url>`)
- Headless web backend: `./python.sh -m voidx.main --web --web-headless`
- Export UI protocol schema: `./python.sh scripts/export_ui_protocol_schema.py`
- Run all tests: `./test.py` (backend + frontend + desktop; use `--backend`/`--frontend`/`--desktop` to select, `--` to pass args to the suite)

## Code Rules
- Keep modules small and named by responsibility.
- Do not add comments unless they explain non-obvious intent or constraints.
- Don't repeat yourself — single source of truth, no duplicated rules across files.

## Document Lifecycle
- Design docs live in `docs/specs/` while in progress.
- When implementation is **fully complete** (code + tests exist, not just stubs or string references), move the doc to `docs/archive/` and add a `> **Status: Done**` header.
- Do **not** archive based on keyword search alone — verify the actual implementation files exist and are functional.
- `docs/design/` is for exploratory/RFC-stage docs; `docs/specs/` is for approved designs awaiting or in implementation.

## Releasing
- Release flow and version file checklist: `docs/releasing.md` (single source of truth — do not duplicate).

## Safety
- Do not commit `.voidx/`, `.env*`, or local credentials.
- Preserve user work in a dirty tree; never revert unrelated changes.
- Run the relevant focused tests before broad test runs.
