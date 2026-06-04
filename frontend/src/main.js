import { describeEvent, renderTranscript } from "./render.js";

const statusEl = document.querySelector("#status");
const transcriptEl = document.querySelector("#transcript");
const eventsEl = document.querySelector("#events");
const composerEl = document.querySelector("#composer");
const inputEl = document.querySelector("#input");
const requestDialogEl = document.querySelector("#request-dialog");
const requestTitleEl = document.querySelector("#request-title");
const requestDetailsEl = document.querySelector("#request-details");
const requestControlsEl = document.querySelector("#request-controls");
let socket = null;

bootstrap().catch((error) => {
  setStatus(error instanceof Error ? error.message : String(error));
});

async function bootstrap() {
  const wsUrl = await resolveWsUrl();
  if (!wsUrl) {
    setStatus("Add ?ws=ws://127.0.0.1:<port>/?token=<token> to connect.");
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
      const url = await invoke("gateway_url");
      if (typeof url === "string" && url) {
        return url;
      }
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
  }
  return null;
}

function connect(url) {
  socket = new WebSocket(url);
  socket.addEventListener("open", () => setStatus("Connected"));
  socket.addEventListener("close", () => setStatus("Disconnected"));
  socket.addEventListener("error", () => setStatus("Connection error"));
  socket.addEventListener("message", (event) => {
    const envelope = JSON.parse(event.data);
    if (envelope.type === "snapshot") {
      renderTranscript(transcriptEl, envelope.payload);
      window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
      return;
    }
    if (envelope.type === "event") {
      appendEvent(envelope);
      return;
    }
    if (envelope.type === "request") {
      showRequest(envelope.payload);
    }
  });
}

composerEl.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = inputEl.value.trim();
  if (!text || !socket || socket.readyState !== WebSocket.OPEN) {
    return;
  }
  socket.send(JSON.stringify({
    type: "command",
    payload: { kind: "submit", text },
  }));
  inputEl.value = "";
});

inputEl.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.metaKey) {
    event.preventDefault();
    composerEl.requestSubmit();
  }
});

function setStatus(text) {
  statusEl.textContent = text;
}

function appendEvent(envelope) {
  const row = document.createElement("div");
  row.textContent = describeEvent(envelope);
  eventsEl.append(row);
  eventsEl.scrollTop = eventsEl.scrollHeight;
}

function showRequest(request) {
  requestTitleEl.textContent = request.prompt;
  requestDetailsEl.replaceChildren();
  requestControlsEl.replaceChildren();

  if (request.kind === "permission" && request.tools?.length) {
    requestDetailsEl.className = "request-details";
    requestDetailsEl.textContent = request.tools
      .map((tool) => `${tool.name} ${tool.pattern || ""}\n${JSON.stringify(tool.args || {}, null, 2)}`)
      .join("\n\n");
  } else {
    requestDetailsEl.className = "";
  }

  if (request.kind === "text") {
    renderTextRequest(request);
  } else {
    renderChoiceRequest(request);
  }

  requestDialogEl.showModal();
}

function renderChoiceRequest(request) {
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
    type: "response",
    payload: { request_id: requestId, value },
  }));
  requestDialogEl.close();
}
