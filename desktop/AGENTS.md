# voidx Desktop Agent Instructions

This subproject is the native desktop shell for voidx — a Tauri 2 application
that spawns the Python backend as a sidecar and hosts the web frontend in a
native window. It does **not** bundle Python; at runtime it resolves the
interpreter and launches `voidx.main --web --web-headless`.

## Project Shape
- `tauri/src/lib.rs`: Pure, testable logic — Python interpreter resolution, workspace resolution, persistence, backend status serialization.
- `tauri/src/main.rs`: Tauri application entry — app state, tauri commands, backend spawn/kill lifecycle, window events. Depends on `lib.rs`.
- `tauri/tests/`: Integration tests mirroring `src/` — one file per concern.
- `tauri/build.rs`: Tauri build script (codegen).
- `tauri/tauri.conf.json`: Tauri config — frontend dist path, CSP, window defaults, bundle targets.
- `tauri/capabilities/default.json`: Tauri 2 capability permissions for the main window.
- `build.sh`: One-click build script — builds frontend then runs `tauri build`.

## Runtime Environment
- Rust toolchain required (`cargo`, `rustc`). The Tauri CLI is provided via
  `node_modules/@tauri-apps/cli` — invoke with `./node_modules/.bin/tauri`.

## Commands
- Dev shell: `npm run dev` (spawns Tauri dev with frontend hot-reload)
- Build bundle: `./build.sh` (frontend + native bundle)
- Build (no frontend): `./build.sh --no-frontend`
- Rust tests: `cd tauri && cargo test`
- Rust check: `cd tauri && cargo check`
- Tauri info: `./node_modules/.bin/tauri info`

## Code Rules
- Keep `lib.rs` free of Tauri dependencies — it holds pure logic that tests
  can exercise without a runtime. Tauri-coupled code stays in `main.rs`.
- Functions in `lib.rs` are `pub` so integration tests under `tauri/tests/`
  can import them.
- `resolve_python` and `resolve_workspace` read env vars and the filesystem;
  tests must isolate via temp dirs and env-var guards (see `HOME_LOCK` in
  `tests/persist_workspace.rs`).
- Do not add comments unless they explain non-obvious intent or constraints.

## Testing
- Framework: Rust built-in (`#[test]`), run via `cargo test`.
- Integration tests live in `tauri/tests/`, one file per concern, mirroring
  the `lib.rs` public API surface.
- Tests that mutate `HOME` must serialize via a `Mutex` guard — `cargo test`
  runs threads in parallel and `std::env::set_var` is process-global.
- `tempfile` crate is in `[dev-dependencies]` for isolated filesystem tests.

## Safety
- Do not commit `tauri/target/` or `tauri/gen/` (build artifacts).
- `tauri/Cargo.lock` **should** be committed (binary application).
- Preserve user work in a dirty tree; never revert unrelated changes.
