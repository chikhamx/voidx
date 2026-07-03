type RpcPending = {
  resolve: (value: unknown) => void;
  reject: (reason: Error) => void;
};

type NotificationHandler = (params: Record<string, unknown>) => void;
type RequestHandler = (params: Record<string, unknown>) => unknown;

let socket: WebSocket | null = null;
let nextId = 1;
const pending = new Map<number, RpcPending>();
const notificationHandlers = new Map<string, NotificationHandler>();
const requestHandlers = new Map<string, RequestHandler>();

export function _setSocket(ws: WebSocket | null): void {
  socket = ws;
  if (ws) {
    if (typeof ws.addEventListener === "function") {
      ws.addEventListener("message", handleMessage);
    } else {
      (ws as unknown as { onmessage: ((ev: MessageEvent) => void) | null }).onmessage =
        handleMessage;
    }
  }
}

export function _resetForTest(): void {
  socket = null;
  nextId = 1;
  pending.clear();
  notificationHandlers.clear();
  requestHandlers.clear();
}

export function rpcCall(method: string, params: Record<string, unknown> = {}): Promise<unknown> {
  return new Promise((resolve, reject) => {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      reject(new Error("socket not connected"));
      return;
    }
    const id = nextId++;
    pending.set(id, { resolve, reject });
    socket.send(JSON.stringify({ jsonrpc: "2.0", id, method, params }));
  });
}

export function rpcNotify(method: string, params: Record<string, unknown> = {}): void {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    return;
  }
  socket.send(JSON.stringify({ jsonrpc: "2.0", method, params }));
}

export function onNotification(method: string, handler: NotificationHandler): void {
  notificationHandlers.set(method, handler);
}

export function onRequest(method: string, handler: RequestHandler): void {
  requestHandlers.set(method, handler);
}

function handleMessage(event: MessageEvent): void {
  let msg: Record<string, unknown>;
  try {
    msg = JSON.parse(event.data as string);
  } catch {
    return;
  }

  if (
    msg.id != null &&
    (msg.method || msg.result !== undefined || msg.error !== undefined)
  ) {
    if (msg.method) {
      handleRequest(msg);
    } else {
      handleResponse(msg);
    }
    return;
  }

  if (msg.method) {
    const handler = notificationHandlers.get(msg.method as string);
    if (handler) {
      handler((msg.params as Record<string, unknown>) || {});
    }
  }
}

function handleResponse(msg: Record<string, unknown>): void {
  const entry = pending.get(msg.id as number);
  if (!entry) {
    return;
  }
  pending.delete(msg.id as number);
  if (msg.error) {
    entry.reject(
      new Error(
        ((msg.error as Record<string, unknown>).message as string) || "RPC error",
      ),
    );
  } else {
    entry.resolve(msg.result);
  }
}

function handleRequest(msg: Record<string, unknown>): void {
  const handler = requestHandlers.get(msg.method as string);
  if (!handler) {
    return;
  }
  Promise.resolve(handler((msg.params as Record<string, unknown>) || {}))
    .then((result) => {
      if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(
          JSON.stringify({ jsonrpc: "2.0", id: msg.id, result }),
        );
      }
    })
    .catch(() => {
      if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(
          JSON.stringify({
            jsonrpc: "2.0",
            id: msg.id,
            error: { code: -32603, message: "request handler error" },
          }),
        );
      }
    });
}
