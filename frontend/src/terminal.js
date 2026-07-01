let inputCb = null;
let startCb = null;
let activeTerminalId = null;
let initialized = false;

export function initTerminal() {
  const pane = document.querySelector("#terminal-pane");
  if (!pane) return;

  if (activeTerminalId) {
    ensureTerminalElements(pane, activeTerminalId);
  } else {
    renderStartButton(pane);
  }
}

function renderStartButton(pane) {
  pane.replaceChildren();
  const btn = document.createElement("button");
  btn.className = "vx-terminal-start";
  btn.textContent = "Start Terminal";
  btn.addEventListener("click", () => {
    if (startCb) startCb();
  });
  pane.append(btn);
}

function ensureTerminalElements(pane, terminalId) {
  if (pane.querySelector(".vx-terminal-output")) return;

  pane.replaceChildren();

  const output = document.createElement("pre");
  output.className = "vx-terminal-output";
  output.dataset.terminalId = terminalId;
  pane.append(output);

  const input = document.createElement("input");
  input.className = "vx-terminal-input";
  input.type = "text";
  input.placeholder = "Type and press Enter to send...";
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      if (inputCb) {
        inputCb(terminalId, input.value);
      }
      input.value = "";
    }
  });
  pane.append(input);
  input.focus();
}

export function appendTerminalOutput(terminalId, data) {
  const pane = document.querySelector("#terminal-pane");
  if (!pane) return;

  if (!activeTerminalId) {
    activeTerminalId = terminalId;
    ensureTerminalElements(pane, terminalId);
  }

  let output = pane.querySelector(".vx-terminal-output");
  if (!output || output.dataset.terminalId !== terminalId) {
    activeTerminalId = terminalId;
    ensureTerminalElements(pane, terminalId);
    output = pane.querySelector(".vx-terminal-output");
  }

  output.textContent += data;
  output.scrollTop = output.scrollHeight;
}

export function showTerminalClosed(terminalId) {
  const pane = document.querySelector("#terminal-pane");
  if (!pane) return;

  const output = pane.querySelector(".vx-terminal-output");
  if (output) {
    const closed = document.createElement("div");
    closed.className = "vx-terminal-closed";
    closed.textContent = `[terminal ${terminalId} closed]`;
    output.append(closed);
  }

  const input = pane.querySelector(".vx-terminal-input");
  if (input) {
    input.disabled = true;
  }
}

export function onTerminalInput(callback) {
  inputCb = callback;
}

export function onTerminalStart(callback) {
  startCb = callback;
}

export function setActiveTerminal(terminalId) {
  activeTerminalId = terminalId;
}

export function _resetForTest() {
  inputCb = null;
  startCb = null;
  activeTerminalId = null;
  initialized = false;
}
