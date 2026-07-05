# voidx Frontend Agent Instructions

This subproject is the web/desktop UI for voidx — a TypeScript SPA that
connects to the Python backend's WebSocket gateway and renders the
conversation transcript, tool outputs, and dock panels. It runs in two
contexts: browser (dev server) and Tauri webview (desktop shell).

## Module Map
- `main.ts`: Entry point — boots the app, wires RPC to UI.
- `rpc.ts` + `rpc-worker.ts`: WebSocket transport (main-thread API + Web Worker).
- `protocol.d.ts` + `protocol.schema.json`: UI protocol types (`.d.ts` is generated — see Schema Sync).
- `render.ts`: Transcript rendering — maps protocol payloads to DOM.
- `stream.ts`: Streaming text accumulation with debounce.
- `markdown.ts`: Markdown → sanitized HTML with code highlighting.
- `slash.ts`: Slash command catalog and autocomplete.
- `dock.ts`: Bottom dock panel — tabs, todo.
- `sidebar.ts`: Session list, search, new-chat.
- `settings.ts`: Settings panel — profiles, permissions.
- `terminal.ts`: Embedded terminal bridge.
- `integrations.ts`: MCP/LSP integration management.
- `context-menu.ts`: Right-click context menu.
- `diff-review.ts`: Diff viewer for file change review.
- `types.ts`: Shared frontend-only types.

## Schema Sync
`src/protocol.d.ts` is generated from `src/protocol.schema.json`. The schema
is exported from the Python side via `scripts/export_ui_protocol_schema.py`.
To regenerate after protocol changes:
```
npm run schema
```
This runs the Python export script then `json-schema-to-typescript` to write
`protocol.d.ts`. Never hand-edit `protocol.d.ts` — it will be overwritten.

## Tauri Integration
- `@tauri-apps/api` and `@tauri-apps/plugin-dialog` are dependencies for the
  desktop context. In browser-only dev they are unused.
- The desktop shell (`desktop/tauri/`) loads this frontend via
  `frontendDist: "../../frontend/dist"` in `tauri.conf.json`.
- `main.ts` detects Tauri vs browser context and adapts accordingly.

## Build & Dev
- Dev server: `npm run dev` (Vite, binds 127.0.0.1)
- Build: `npm run build` (outputs to `dist/`)
- Preview built dist: `npm run preview`

## TypeScript
- `tsconfig.json`: strict mode, ES2020 target, bundler module resolution.
- `noEmit: true` — type-checking only, Vite handles emission.
- Path aliases: none — all imports are relative.

## Code Rules
- Export private functions that tests need to reach; guard module-top-level
  side effects (e.g. `bootstrap()`) with `import.meta.env.TEST` so importing
  under vitest stays pure.

## Testing
- vitest + jsdom.
- `test/setup.ts` runs at module top level — injects the DOM skeleton.
- Test files live in `test/`, named `<module>.test.ts`, mirroring `src/<module>.ts`.
- Globals are enabled (`globals: true`) — `describe`/`it`/`expect` are
  available without import.
- Stateful modules expose a `_resetForTest()` export to clear module-level
  state in `beforeEach`.
- Run all: `npm test` · Run focused: `npx vitest run test/<module>.test.ts`