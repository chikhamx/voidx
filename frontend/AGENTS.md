# voidx Frontend Agent Instructions

## Module Map
- `main.ts`: Entry point — boots the app, wires RPC to UI.
- `rpc/`: WebSocket transport module.
  - `client.ts`: Main-thread client API for sending and receiving RPC messages.
  - `worker.ts`: Web Worker managing WebSocket connection logic.
  - `protocol.d.ts` + `protocol.schema.json`: UI protocol types (`.d.ts` is generated — see Schema Sync).
  - `index.ts`: Public export gateway.
- `services/`: Core services and application state.
  - `state.ts`: UI state management, status bar updater, and DOM element caches.
  - `connection.ts`: Socket connection/reconnection and workspace picker lifecycle.
  - `index.ts`: Public export gateway.
- `ui/`: User interface components and panel controllers.
  - `sidebar.ts`: Session threads list, workspace categories, search, rename, and new chats.
  - `dock.ts`: Tabs manager for Todo/Terminal panels.
  - `dialog.ts`: Permission/Tool execution approval popup dialogs.
  - `workspace.ts`: Project switcher drawer and sidebar resize handles.
  - `model.ts`: Custom model dropdown picker and permission toggles.
  - `settings.ts` / `integrations.ts` / `context-menu.ts` / `diff-review.ts` / `terminal.ts` / `slash.ts`: Specilized UI overlays.
  - `index.ts`: Public export gateway.
- `utils/`: Parsing, streaming, and rendering helpers.
  - `render.ts`: Localized English text generators and tool group renderers.
  - `stream.ts`: Debounced thinking stream and assistant text block collectors.
  - `markdown.ts`: Syntax highlight sanitizer.
  - `types.ts`: Sibling shared Typescript definitions.
  - `index.ts`: Public export gateway.
- `css/`: Modular components styling.
  - `styles.css`: CSS Entry point importing all sub-stylesheets.
  - `tokens.css`: Visual tokens (colors, variables, and themes).
  - `base.css`: Standard resets, notice toasts, status dots, and markdown typography.
  - `layout.css`: Shell layouts, sidebar resizer, and responsive breakpoints.
  - `chat.css`: Messages, bubble themes, tool logs, brain thoughts, and headers.
  - `composer.css`: Composer inputs, model selects, and permission selectors.
  - `components.css`: Todo lists, terminals, diff reviewers, context menus, and status grids.

## Schema Sync
`src/protocol.d.ts` is generated from `src/protocol.schema.json`. The schema
is exported from the Python side via `scripts/export_ui_protocol_schema.py`.
To regenerate after protocol changes:
```
npm run schema
```
Never hand-edit `protocol.d.ts` — it will be overwritten.

## Tauri Integration
- `@tauri-apps/api` and `@tauri-apps/plugin-dialog` are dependencies for the desktop context.
- The desktop shell (`desktop/tauri/`) loads this frontend via
  `frontendDist: "../../frontend/dist"` in `tauri.conf.json`.
- `main.ts` detects Tauri vs browser context and adapts accordingly.

## Build & Dev
- Dev server: `cd frontend && npm run dev` (Vite, binds 127.0.0.1)
- Build: `cd frontend && npm run build` (outputs to `dist/`)
- Preview built dist: `cd frontend && npm run preview`

## TypeScript
- `tsconfig.json`: strict mode, ES2020 target, bundler module resolution.

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
- Run all: `./test.py --frontend` · Run focused: `./test.py --frontend -- test/<module>.test.ts`