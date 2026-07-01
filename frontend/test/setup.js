import { beforeEach } from "vitest";

document.body.innerHTML = `
  <main class="vx-shell">
    <header class="vx-titlebar">
      <div class="vx-titlebar-left">
        <span class="status-dot disconnected" id="status-dot"></span>
        <span class="vx-brand">voidx</span>
        <span class="status-workspace" id="status-workspace"></span>
      </div>
      <div class="vx-titlebar-center">
        <input type="text" class="vx-search" id="session-search" placeholder="Search sessions..." />
      </div>
      <div class="vx-titlebar-right">
        <span class="status-model" id="status-model"></span>
      </div>
    </header>
    <div class="vx-body">
      <aside class="vx-sidebar" id="sidebar">
        <div class="vx-sidebar-header">
          <button class="vx-new-chat" id="btn-new-chat">+ New</button>
        </div>
        <div class="vx-session-list" id="session-list"></div>
      </aside>
      <section class="vx-main">
        <div class="transcript" id="transcript" aria-live="polite"></div>
        <form class="composer" id="composer">
          <div class="slash-menu" id="slash-menu"></div>
          <textarea id="input" rows="3"></textarea>
          <div class="vx-composer-actions">
            <button type="submit" class="btn-send" id="btn-send">Send</button>
            <button type="button" class="btn-cancel" id="btn-cancel" disabled>Cancel</button>
          </div>
        </form>
      </section>
      <aside class="vx-dock" id="dock">
        <div class="vx-dock-tabs">
          <button class="vx-dock-tab active" data-tab="todo">Todo</button>
          <button class="vx-dock-tab" data-tab="terminal">Terminal</button>
          <button class="vx-dock-tab" data-tab="diff">Diff</button>
          <button class="vx-dock-toggle" id="dock-toggle">▾</button>
        </div>
        <div class="vx-dock-content" id="dock-content">
          <div class="vx-dock-pane" data-pane="todo">
            <section class="todo-panel" id="todo-panel" aria-label="Task progress"></section>
          </div>
          <div class="vx-dock-pane" data-pane="terminal" hidden>
            <div class="vx-terminal" id="terminal-pane"></div>
          </div>
          <div class="vx-dock-pane" data-pane="diff" hidden>
            <div class="vx-diff-review" id="diff-pane"></div>
          </div>
        </div>
      </aside>
    </div>
    <footer class="vx-statusbar">
      <span class="status-session" id="status-session"></span>
    </footer>
    <dialog id="request-dialog" class="request-dialog">
      <form id="request-form" method="dialog">
        <h2 id="request-title"></h2>
        <div id="request-details"></div>
        <div id="request-controls"></div>
      </form>
    </dialog>
  </main>
`;

beforeEach(() => {
  const transcript = document.querySelector("#transcript");
  if (transcript) transcript.innerHTML = "";
  const todo = document.querySelector("#todo-panel");
  if (todo) todo.innerHTML = "";
  const slash = document.querySelector("#slash-menu");
  if (slash) slash.innerHTML = "";
  const input = document.querySelector("#input");
  if (input) input.value = "";
});
