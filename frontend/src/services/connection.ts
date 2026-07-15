import { createWorkerSocket, rpcCall, _setSocket } from "../rpc";
import {
  uiState,
  setConnectionStatus,
  setRunning,
  updateStatusBar,
  syncEmptyState,
  transcriptEl,
} from "./state";
import type { SettingsSnapshot } from "../ui/settings";

export let socket: ReturnType<typeof createWorkerSocket> | null = null;
export let reconnectAttempts = 0;
export const MAX_RECONNECT = 10;
export let startupSettingsRequested = false;
export let connectionGeneration = 0;

export function _resetConnectionForTest(): void {
  socket = null;
  reconnectAttempts = 0;
  startupSettingsRequested = false;
  connectionGeneration = 0;
}

export function incrementConnectionGeneration(): void {
  connectionGeneration += 1;
}

export function setSocket(s: typeof socket): void {
  socket = s;
}

export function setReconnectAttempts(val: number): void {
  reconnectAttempts = val;
}

export function setStartupSettingsRequested(val: boolean): void {
  startupSettingsRequested = val;
}

export async function bootstrap(): Promise<void> {
  const wsUrl = await resolveWsUrl();
  if (!wsUrl) {
    setConnectionStatus(
      "disconnected",
      "Add ?ws=ws://127.0.0.1:<port>/?token=[redacted] to connect.",
    );
    return;
  }
  connect(wsUrl);
}

export async function resolveWsUrl(): Promise<string | null> {
  const params = new URLSearchParams(window.location.search);
  const direct = params.get("ws");
  if (direct) {
    return direct;
  }
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    for (let attempt = 0; attempt < 60; attempt += 1) {
      const url: unknown = await invoke("get_gateway_url");
      if (typeof url === "string" && url) {
        return url;
      }
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
  } catch {
    return null;
  }
  return null;
}

export function connect(url: string): void {
  const generation = connectionGeneration;
  setConnectionStatus("connecting");
  socket = createWorkerSocket(url);
  let reconnecting = false;
  const scheduleReconnect = () => {
    if (generation !== connectionGeneration) {
      return;
    }
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
  _setSocket(socket);
  socket.addEventListener("close", () => {
    if (generation !== connectionGeneration) {
      return;
    }
    setConnectionStatus("disconnected");
    scheduleReconnect();
  });
  socket.addEventListener("error", () => {
    if (generation !== connectionGeneration) {
      return;
    }
    setConnectionStatus("disconnected", "Connection error");
  });
}

export async function switchWorkspace(workspace: string): Promise<void> {
  connectionGeneration += 1;
  uiState.workspace = workspace;
  uiState.sessionId = "";
  uiState.isRunning = false;
  transcriptEl.replaceChildren();
  syncEmptyState();
  setConnectionStatus("connecting");
  updateStatusBar();

  if (socket) {
    socket.close();
    _setSocket(null);
  }

  const { invoke } = await import("@tauri-apps/api/core");
  await invoke("restart_backend", { workspace });
  const url = await resolveWsUrl();
  if (url) {
    connect(url);
  }
}

export function requestStartupSettingsIfNeeded(
  applySettingsRuntimeState: (snapshot: SettingsSnapshot) => void,
): void {
  if (startupSettingsRequested || uiState.configuredProfiles.length > 0) {
    return;
  }
  startupSettingsRequested = true;
  rpcCall("settings.get", {})
    .then((snapshot) => {
      applySettingsRuntimeState(snapshot as SettingsSnapshot);
    })
    .catch((error: Error) => {
      console.warn("voidx: startup settings fallback failed", error.message);
    });
}
