type WorkerCommand =
  | { type: "connect"; url: string }
  | { type: "send"; data: string }
  | { type: "close" };

let socket: WebSocket | null = null;

function closeSocket(): void {
  if (socket) {
    socket.close();
    socket = null;
  }
}

self.addEventListener("message", (event: MessageEvent<WorkerCommand>) => {
  const command = event.data;
  if (command.type === "connect") {
    closeSocket();
    socket = new WebSocket(command.url);
    socket.addEventListener("open", () => self.postMessage({ type: "open" }));
    socket.addEventListener("close", () => self.postMessage({ type: "close" }));
    socket.addEventListener("error", () => self.postMessage({ type: "error" }));
    socket.addEventListener("message", (messageEvent) => {
      self.postMessage({ type: "message", data: String(messageEvent.data) });
    });
  } else if (command.type === "send") {
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send(command.data);
    }
  } else if (command.type === "close") {
    closeSocket();
  }
});

export {};
