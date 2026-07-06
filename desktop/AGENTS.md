# voidx Desktop Agent Instructions

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
- Dev shell: `cd desktop && npm run dev` (spawns Tauri dev with frontend hot-reload)
- Build bundle: `cd desktop && ./build.sh` (frontend + native bundle)
- Build (no frontend): `cd desktop && ./build.sh --no-frontend`
- Rust tests: `cd desktop/tauri && cargo test`
- Rust check: `cd desktop/tauri && cargo check`
- Tauri info: `cd desktop && ./node_modules/.bin/tauri info`

## Code Rules
- Keep `lib.rs` free of Tauri dependencies — it holds pure logic that tests
  can exercise without a runtime. Tauri-coupled code stays in `main.rs`.
- Functions in `lib.rs` are `pub` so integration tests under `tauri/tests/`
  can import them.
- Tests that mutate env vars must serialize via a `Mutex` guard (see `tests/`).
- Do not add comments unless they explain non-obvious intent or constraints.

## Testing
- Integration tests in `tauri/tests/`, one file per concern, mirroring `lib.rs`.
- `cargo test` runs threads in parallel; env-mutating tests need a `Mutex` guard.

## Safety
- Do not commit `tauri/target/` or `tauri/gen/` (build artifacts).
- `tauri/Cargo.lock` **should** be committed (binary application).
- Preserve user work in a dirty tree; never revert unrelated changes.
