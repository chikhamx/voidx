import { renderTranscript, renderTodoPanel } from "./render.js";
import { matchSlashCommands, renderSlashMenu } from "./slash.js";
import { setTranscriptElement, appendStreamText, commitStream, discardStream } from "./stream.js";
import { renderMarkdown, renderUserMessage } from "./markdown.js";

const statusDotEl = document.querySelector("#status-dot");
const statusModelEl = document.querySelector("#status-model");
const statusWorkspaceEl = document.querySelector("#status-workspace");
const statusSessionEl = document.querySelector("#status-session");
const transcriptEl = document.querySelector("#transcript");
const todoPanelEl = document.querySelector("#todo-panel");
const composerEl = document.querySelector("#composer");
const inputEl = document.querySelector("#input");
const btnSendEl = document.querySelector("#btn-send");
const btnCancelEl = document.querySelector("#btn-cancel");
const slashMenuEl = document.querySelector("#slash-menu");
const requestDialogEl = document.querySelector("#request-dialog");
const requestTitleEl = document.querySelector("#request-title");
const requestDetailsEl = document.querySelector("#request-details");
const requestControlsEl = document.querySelector("#request-controls");

const uiState = {
  connection: "disconnected",
  model: "",
  workspace: "",
  sessionId: "",
  isRunning: false,
  slashCommands: [],
  slashSelectedIndex: 0,
};

let socket = null;
let reconnectAttempts = 0;
const MAX_RECONNECT = 10;

setTranscriptElement(transcriptEl);

if (!import.meta.env.TEST) {
  bootstrap().catch((error) => {
    setConnectionStatus("error", error instanceof Error ? error.message : String(error));
  });
}

async function bootstrap() {
  const wsUrl = await resolveWsUrl();
  if (!wsUrl) {
    setConnectionStatus("disconnected", "Add ?ws=ws://127.0.0.1:<port>/?token=xxx to connect.");
    return;
  }
  connect(wsUrl);
}

async function resolveWsUrl() {
  const params = new URLSearchParams(window.location.search);
  const direct = params.get("ws");
  if (direct) {
    return direct;
  }
  if (window.__TAURI_INTERNALS__ || window.__TAURI__) {
    const { invoke } = await import("@tauri-apps/api/core");
    for (let attempt = 0; attempt < 60; attempt += 1) {
      const url = await invoke("get_gateway_url");
      if (typeof url === "string" && url) {
        return url;
      }
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
  }
  return null;
}

function connect(url) {
  setConnectionStatus("connecting");
  socket = new WebSocket(url);
  let reconnecting = false;
  const scheduleReconnect = () => {
    if (reconnecting) {
      return;
    }
    reconnecting = true;
    setRunning(false);
    if (reconnectAttempts < MAX_RECONNECT) {
      reconnectAttempts += 1;
      setTimeout(() => connect(url), 5000);
    }
  };
  socket.addEventListener("open", () => {
    reconnectAttempts = 0;
    setConnectionStatus("connected");
  });
  socket.addEventListener("close", () => {
    setConnectionStatus("disconnected");
    scheduleReconnect();
  });
  socket.addEventListener("error", () => {
    setConnectionStatus("disconnected", "Connection error");
  });
  socket.addEventListener("message", (event) => {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch {
      console.warn("voidx: ignoring non-JSON websocket message");
      return;
    }
    const method = msg.method;
    const params = msg.params || {};

    if (method === "workspace.snapshot") {
      const snapshot = params.active_snapshot || { nodes: [] };
      uiState.sessionId = params.active_thread_id || "";
      updateStatusBar();
      renderTranscript(transcriptEl, snapshot);
      scrollToBottom();
      return;
    }
    if (method === "ui.request") {
      showRequest(params);
      return;
    }
    if (method === "startup.shown") {
      uiState.model = params.model || "";
      uiState.workspace = params.workspace || "";
      updateStatusBar();
      return;
    }
    if (method === "turn.started") {
      setRunning(true);
      return;
    }
    if (method === "capture.started" || method === "capture.stopped") {
      return;
    }
    if (method === "refresh.requested" || method === "reset.requested") {
      return;
    }
    if (method === "notice.set") {
      return;
    }
    if (method === "input.set") {
      if (params.text) {
        inputEl.value = params.text;
      }
      return;
    }
    if (method === "item.started" || method === "item.delta" || method === "item.completed") {
      handleItem(method, params);
      return;
    }
  });
}

export function handleItem(method, params) {
  const kind = params.kind;
  const itemId = params.item_id;
  const data = params.data || {};

  if (kind === "assistant_stream") {
    if (method === "item.started") {
      appendStreamText(itemId, "", data.phase || "text");
    } else if (method === "item.delta") {
      appendStreamText(itemId, data.text || "", data.phase || "text");
    } else if (method === "item.completed") {
      commitStream(itemId);
    }
    return;
  }
  if (kind === "tool") {
    handleToolItem(method, itemId, data);
    return;
  }
  if (kind === "todo") {
    if (method === "item.started") {
      renderTodoPanel(todoPanelEl, data.items || [], data.summary || "");
    } else if (method === "item.completed") {
      if (data.cleared) {
        renderTodoPanel(todoPanelEl, [], "");
      }
    }
    return;
  }
  if (kind === "prompt") {
    if (method === "item.completed" && data.cleared) {
      requestDialogEl.close();
    }
    return;
  }
  if (kind === "status") {
    return;
  }
  if (kind === "subagent") {
    return;
  }
  if (kind === "message") {
    if (method === "item.started") {
      appendMessageItem(itemId, data);
    }
    return;
  }
}

export function appendMessageItem(itemId, data) {
  const el = document.createElement("div");
  el.className = `message-item message-${data.style || "text"}`;
  el.dataset.itemId = itemId;
  const text = data.text || "";
  if (data.style === "markdown" || data.style === "guidance") {
    el.append(renderMarkdown(text));
  } else if (data.style === "text") {
    el.append(renderUserMessage(text));
  } else {
    const pre = document.createElement("pre");
    pre.textContent = text;
    el.append(pre);
  }
  if (transcriptEl) {
    transcriptEl.append(el);
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
  }
}


export function handleToolItem(method, itemId, data) {
  let el = document.querySelector(`[data-tool-id="${data.tool_call_id}"]`);
  if (method === "item.started") {
    setRunning(true);
    el = document.createElement("div");
    el.className = "tool-item";
    el.dataset.toolId = data.tool_call_id;
    el.dataset.itemId = itemId;

    const header = document.createElement("div");
    header.className = "tool-header";
    const name = document.createElement("span");
    name.className = "tool-name";
    name.textContent = data.tool_name || data.label || "tool";
    const spinner = document.createElement("span");
    spinner.className = "tool-spinner";
    spinner.textContent = "running";
    header.append(name, spinner);
    el.append(header);

    if (data.args) {
      const args = document.createElement("pre");
      args.className = "tool-args";
      args.textContent = typeof data.args === "string"
        ? data.args
        : JSON.stringify(data.args, null, 2);
      el.append(args);
    }

    if (transcriptEl) {
      transcriptEl.append(el);
      transcriptEl.scrollTop = transcriptEl.scrollHeight;
    }
  } else if (el) {
    if (method === "item.delta") {
      if (data.diff_text) {
        const diff = document.createElement("pre");
        diff.className = "tool-diff";
        diff.textContent = data.diff_text;
        el.append(diff);
      } else if (data.detail) {
        const detail = document.createElement("pre");
        detail.className = "tool-detail";
        detail.textContent = data.detail;
        el.append(detail);
      }
    } else if (method === "item.completed") {
      const spinner = el.querySelector(".tool-spinner");
      if (spinner) {
        spinner.textContent = data.ok ? "done" : "failed";
        spinner.className = `tool-spinner ${data.ok ? "ok" : "err"}`;
      }
      if (data.detail) {
        const detail = document.createElement("pre");
        detail.className = "tool-detail";
        detail.textContent = data.detail;
        el.append(detail);
      }
    }
    if (transcriptEl) {
      transcriptEl.scrollTop = transcriptEl.scrollHeight;
    }
  }
}

function setRunning(running) {
  uiState.isRunning = running;
  btnCancelEl.disabled = !running;
  btnSendEl.disabled = running;
}

function setConnectionStatus(status, message) {
  uiState.connection = status;
  statusDotEl.className = `status-dot ${status}`;
  if (message && status === "disconnected") {
    statusModelEl.textContent = message;
  } else if (status === "connected") {
    statusModelEl.textContent = "";
  }
}

function updateStatusBar() {
  if (uiState.model) {
    statusModelEl.textContent = uiState.model;
  }
  if (uiState.workspace) {
    const short = uiState.workspace.replace(/^.*[\\/]/, "");
    statusWorkspaceEl.textContent = short;
  }
  if (uiState.sessionId) {
    statusSessionEl.textContent = `session ${uiState.sessionId.slice(0, 8)}`;
  }
}

function scrollToBottom() {
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

composerEl.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = inputEl.value.trim();
  if (!text || !socket || socket.readyState !== WebSocket.OPEN) {
    return;
  }
  socket.send(JSON.stringify({
    jsonrpc: "2.0",
    id: Date.now(),
    method: "session.submit",
    params: { text },
  }));
  appendMessageItem(`user-${Date.now()}`, { style: "text", text });
  inputEl.value = "";
  hideSlashMenu();
});

btnCancelEl.addEventListener("click", () => {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    return;
  }
  socket.send(JSON.stringify({
    jsonrpc: "2.0",
    id: Date.now(),
    method: "session.cancel",
    params: {},
  }));
  setRunning(false);
});

inputEl.addEventListener("keydown", (event) => {
  if (uiState.slashCommands.length > 0) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      uiState.slashSelectedIndex = (uiState.slashSelectedIndex + 1) % uiState.slashCommands.length;
      updateSlashMenu();
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      uiState.slashSelectedIndex = (uiState.slashSelectedIndex - 1 + uiState.slashCommands.length) % uiState.slashCommands.length;
      updateSlashMenu();
      return;
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      const selected = uiState.slashCommands[uiState.slashSelectedIndex];
      if (selected) {
        inputEl.value = selected.command + " ";
      }
      hideSlashMenu();
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      hideSlashMenu();
      return;
    }
  }
  if (event.key === "Enter" && !event.shiftKey && !event.metaKey) {
    event.preventDefault();
    composerEl.requestSubmit();
  }
});

inputEl.addEventListener("input", () => {
  const value = inputEl.value;
  if (value.startsWith("/")) {
    const matched = matchSlashCommands(value);
    if (matched.length > 0) {
      uiState.slashCommands = matched;
      uiState.slashSelectedIndex = 0;
      showSlashMenu();
      return;
    }
  }
  hideSlashMenu();
});

function showSlashMenu() {
  updateSlashMenu();
  slashMenuEl.classList.add("visible");
}

function hideSlashMenu() {
  slashMenuEl.classList.remove("visible");
  uiState.slashCommands = [];
  uiState.slashSelectedIndex = 0;
}

function updateSlashMenu() {
  const menu = renderSlashMenu(uiState.slashCommands, uiState.slashSelectedIndex);
  slashMenuEl.replaceChildren(...menu.childNodes);
}

function showRequest(request) {
  requestTitleEl.textContent = request.prompt;
  requestDetailsEl.replaceChildren();
  requestControlsEl.replaceChildren();

  if (request.kind === "permission") {
    renderPermissionDetails(request);
    renderChoiceButtons(request);
  } else if (request.kind === "choice") {
    requestDetailsEl.className = "";
    renderChoiceButtons(request);
  } else if (request.kind === "text") {
    requestDetailsEl.className = "";
    renderTextRequest(request);
  }

  requestDialogEl.showModal();
}

function renderPermissionDetails(request) {
  requestDetailsEl.className = "request-details";
  if (!request.tools?.length) {
    requestDetailsEl.textContent = "";
    return;
  }
  requestDetailsEl.textContent = request.tools
    .map((tool) => `${tool.name} ${tool.pattern || ""}\n${JSON.stringify(tool.args || {}, null, 2)}`)
    .join("\n\n");
}

function renderChoiceButtons(request) {
  const actions = document.createElement("div");
  actions.className = "request-actions";
  for (const [label, value, desc] of request.choices || []) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = desc || label;
    button.addEventListener("click", () => sendResponse(request.request_id, value));
    actions.append(button);
  }
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.textContent = "Cancel";
  cancel.addEventListener("click", () => sendResponse(request.request_id, null));
  actions.append(cancel);
  requestControlsEl.append(actions);
}

function renderTextRequest(request) {
  const input = document.createElement("textarea");
  input.rows = 3;
  input.value = request.default || "";
  input.placeholder = request.secret ? "Input hidden in terminal UI" : "";
  const actions = document.createElement("div");
  actions.className = "request-actions";
  const submit = document.createElement("button");
  submit.type = "button";
  submit.textContent = "Submit";
  submit.addEventListener("click", () => sendResponse(request.request_id, input.value));
  actions.append(submit);
  requestControlsEl.append(input, actions);
  setTimeout(() => input.focus(), 0);
}

function sendResponse(requestId, value) {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    return;
  }
  socket.send(JSON.stringify({
    jsonrpc: "2.0",
    id: requestId,
    result: { value },
  }));
  requestDialogEl.close();
}
