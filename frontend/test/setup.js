import { beforeEach } from "vitest";

document.body.innerHTML = `
  <main class="shell">
    <header class="status-bar">
      <div class="status-item">
        <span class="status-dot disconnected" id="status-dot"></span>
        <span class="status-brand">voidx</span>
      </div>
      <div class="status-item status-model" id="status-model"></div>
      <div class="status-item status-workspace" id="status-workspace"></div>
      <div class="status-item status-session" id="status-session"></div>
    </header>
    <section class="todo-panel" id="todo-panel" aria-label="Task progress"></section>
    <section class="transcript" id="transcript" aria-live="polite"></section>
    <form class="composer" id="composer">
      <div class="slash-menu" id="slash-menu"></div>
      <textarea id="input" rows="3"></textarea>
      <button type="submit" class="btn-send" id="btn-send">Send</button>
      <button type="button" class="btn-cancel" id="btn-cancel" disabled>Cancel</button>
    </form>
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
