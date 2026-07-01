let socket = null;
let nextId = 1;
const pending = new Map();
const notificationHandlers = new Map();
const requestHandlers = new Map();

export function _setSocket(ws) {
  socket = ws;
  if (ws) {
    if (typeof ws.addEventListener === "function") {
      ws.addEventListener("message", handleMessage);
    } else {
      ws.onmessage = handleMessage;
    }
  }
}

export function _resetForTest() {
  socket = null;
  nextId = 1;
  pending.clear();
  notificationHandlers.clear();
  requestHandlers.clear();
}

export function rpcCall(method, params = {}) {
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

export function rpcNotify(method, params = {}) {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    return;
  }
  socket.send(JSON.stringify({ jsonrpc: "2.0", method, params }));
}

export function onNotification(method, handler) {
  notificationHandlers.set(method, handler);
}

export function onRequest(method, handler) {
  requestHandlers.set(method, handler);
}

function handleMessage(event) {
  let msg;
  try {
    msg = JSON.parse(event.data);
  } catch {
    return;
  }

  if (msg.id != null && (msg.method || msg.result !== undefined || msg.error !== undefined)) {
    if (msg.method) {
      handleRequest(msg);
    } else {
      handleResponse(msg);
    }
    return;
  }

  if (msg.method) {
    const handler = notificationHandlers.get(msg.method);
    if (handler) {
      handler(msg.params || {});
    }
  }
}

function handleResponse(msg) {
  const entry = pending.get(msg.id);
  if (!entry) {
    return;
  }
  pending.delete(msg.id);
  if (msg.error) {
    entry.reject(new Error(msg.error.message || "RPC error"));
  } else {
    entry.resolve(msg.result);
  }
}

function handleRequest(msg) {
  const handler = requestHandlers.get(msg.method);
  if (!handler) {
    return;
  }
  Promise.resolve(handler(msg.params || {}))
    .then((result) => {
      if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ jsonrpc: "2.0", id: msg.id, result }));
      }
    })
    .catch(() => {
      if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({
          jsonrpc: "2.0",
          id: msg.id,
          error: { code: -32603, message: "request handler error" },
        }));
      }
    });
}
