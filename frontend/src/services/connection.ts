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


interface DesktopRuntimeWindow {
  __TAURI_INTERNALS__?: unknown;
  __TAURI__?: unknown;
  location?: { protocol?: string };
}

export function isDesktopRuntime(target: DesktopRuntimeWindow = window): boolean {
  return Boolean(
    target.__TAURI_INTERNALS__ ||
    target.__TAURI__ ||
    target.location?.protocol === "tauri:",
  );
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
  if (!import.meta.env.TEST && !isDesktopRuntime()) {
    return null;
  }

  let finished = false;
  let unlisten: (() => void) | null = null;
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    const url = await invoke<unknown>("wait_gateway_url");
    if (typeof url === "string" && url) return url;
  } catch {
    // Older desktop shells fall back to the event and polling paths below.
  }
  const readyEvent = import("@tauri-apps/api/event")
    .then(({ listen }) => new Promise<string>(async (resolve) => {
      unlisten = await listen<{ url?: unknown }>("backend_ready", (event) => {
        const url = event.payload?.url;
        if (typeof url === "string" && url) resolve(url);
      });
    }))
    .catch(() => new Promise<string>(() => {}));

  const polling = (async (): Promise<string | null> => {
    const { invoke } = await import("@tauri-apps/api/core");
    while (!finished) {
      let url: unknown = null;
      try {
        url = await invoke("get_gateway_url");
      } catch {
        await new Promise((resolve) => setTimeout(resolve, 500));
        continue;
      }
      if (typeof url === "string" && url) {
        return url;
      }
      let status: unknown = null;
      try {
        status = await invoke("get_backend_status");
      } catch {
        // Older desktop shells may not expose status; URL polling remains authoritative.
      }
      if (
        status &&
        typeof status === "object" &&
        (status as { status?: unknown }).status === "failed"
      ) {
        const error = (status as { error?: unknown }).error;
        throw new Error(typeof error === "string" && error ? error : "Desktop backend failed to start");
      }
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    return null;
  })();

  const url = await Promise.race([readyEvent, polling]);
  finished = true;
  unlisten?.();
  return url;
}

export function connect(url: string): void {
  const generation = connectionGeneration;
  setConnectionStatus("connecting");
  const sock = createWorkerSocket(url);
  socket = sock;
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
  sock.addEventListener("open", () => {
    reconnectAttempts = 0;
    setConnectionStatus("connected");
  });
  _setSocket(sock);
  sock.addEventListener("close", () => {
    if (generation !== connectionGeneration) {
      return;
    }
    setConnectionStatus("disconnected");
    scheduleReconnect();
  });
  sock.addEventListener("error", () => {
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
