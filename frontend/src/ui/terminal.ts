type OnInput = (terminalId: string, text: string) => void;
type OnStart = () => void;

let inputCb: OnInput | null = null;
let startCb: OnStart | null = null;
let activeTerminalId: string | null = null;

export function initTerminal(): void {
  const pane = document.querySelector<HTMLElement>("#terminal-pane");
  if (!pane) return;

  if (activeTerminalId) {
    ensureTerminalElements(pane, activeTerminalId);
  } else {
    renderStartButton(pane);
  }
}

function renderStartButton(pane: HTMLElement): void {
  pane.replaceChildren();
  const btn = document.createElement("button");
  btn.className = "vx-terminal-start";
  btn.textContent = "Start Terminal";
  btn.addEventListener("click", () => {
    if (startCb) startCb();
  });
  pane.append(btn);
}

function ensureTerminalElements(pane: HTMLElement, terminalId: string): void {
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
  input.addEventListener("keydown", (e: KeyboardEvent) => {
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

export function appendTerminalOutput(terminalId: string, data: string): void {
  const pane = document.querySelector<HTMLElement>("#terminal-pane");
  if (!pane) return;

  if (!activeTerminalId) {
    activeTerminalId = terminalId;
    ensureTerminalElements(pane, terminalId);
  }

  let output = pane.querySelector<HTMLElement>(".vx-terminal-output");
  if (!output || output.dataset.terminalId !== terminalId) {
    activeTerminalId = terminalId;
    ensureTerminalElements(pane, terminalId);
    output = pane.querySelector<HTMLElement>(".vx-terminal-output");
  }
  if (output) {
    output.textContent = (output.textContent ?? "") + data;
    output.scrollTop = output.scrollHeight;
  }
}

export function showTerminalClosed(terminalId: string): void {
  const pane = document.querySelector<HTMLElement>("#terminal-pane");
  if (!pane) return;

  const output = pane.querySelector<HTMLElement>(".vx-terminal-output");
  if (output) {
    const closed = document.createElement("div");
    closed.className = "vx-terminal-closed";
    closed.textContent = `[terminal ${terminalId} closed]`;
    output.append(closed);
  }

  const input = pane.querySelector<HTMLInputElement>(".vx-terminal-input");
  if (input) {
    input.disabled = true;
  }
}

export function onTerminalInput(callback: OnInput): void {
  inputCb = callback;
}

export function onTerminalStart(callback: OnStart): void {
  startCb = callback;
}

export function setActiveTerminal(terminalId: string): void {
  activeTerminalId = terminalId;
}

export function _resetForTest(): void {
  inputCb = null;
  startCb = null;
  activeTerminalId = null;
}
