// @ts-nocheck
import { beforeEach, describe, it, expect, vi } from "vitest";
import { rpcCall, rpcNotify, onNotification, onRequest, _setSocket, _resetForTest, createWorkerSocket, isRpcConnected } from "../src/rpc";

describe("rpc", () => {
  let sentMessages;
  let mockSocket;

  beforeEach(() => {
    _resetForTest();
    sentMessages = [];
    mockSocket = {
      readyState: WebSocket.OPEN,
      send: (data) => sentMessages.push(JSON.parse(data)),
    };
    _setSocket(mockSocket);
  });

  describe("rpcCall", () => {
    it("sends a JSON-RPC request with an id", () => {
      rpcCall("session.list", {});
      expect(sentMessages).toHaveLength(1);
      expect(sentMessages[0].jsonrpc).toBe("2.0");
      expect(sentMessages[0].method).toBe("session.list");
      expect(typeof sentMessages[0].id).toBe("number");
    });

    it("resolves with result when response arrives", async () => {
      const promise = rpcCall("session.list", {});
      const id = sentMessages[0].id;

      const response = {
        jsonrpc: "2.0",
        id,
        result: { threads: [] },
      };
      mockSocket.onmessage({ data: JSON.stringify(response) });

      const result = await promise;
      expect(result).toEqual({ threads: [] });
    });

    it("rejects with error when response has error", async () => {
      const promise = rpcCall("session.switch", { thread_id: "abc" });
      const id = sentMessages[0].id;

      const response = {
        jsonrpc: "2.0",
        id,
        error: { code: -32000, message: "ERR_TURN_IN_PROGRESS" },
      };
      mockSocket.onmessage({ data: JSON.stringify(response) });

      await expect(promise).rejects.toThrow("ERR_TURN_IN_PROGRESS");
    });

    it("does not send when socket is not open", async () => {
      mockSocket.readyState = WebSocket.CLOSED;
      await expect(rpcCall("session.list", {})).rejects.toThrow();
      expect(sentMessages).toHaveLength(0);
    });
  });

  describe("rpcNotify", () => {
    it("sends a notification without an id", () => {
      rpcNotify("refresh.requested", {});
      expect(sentMessages).toHaveLength(1);
      expect(sentMessages[0].jsonrpc).toBe("2.0");
      expect(sentMessages[0].method).toBe("refresh.requested");
      expect(sentMessages[0].id).toBeUndefined();
    });

    it("does not send when socket is not open", () => {
      mockSocket.readyState = WebSocket.CLOSED;
      rpcNotify("refresh.requested", {});
      expect(sentMessages).toHaveLength(0);
    });
  });

  describe("onNotification", () => {
    it("calls registered handler for matching method", () => {
      const handler = vi.fn();
      onNotification("workspace.snapshot", handler);

      const msg = {
        jsonrpc: "2.0",
        method: "workspace.snapshot",
        params: { threads: [] },
      };
      mockSocket.onmessage({ data: JSON.stringify(msg) });

      expect(handler).toHaveBeenCalledWith({ threads: [] });
    });

    it("does not call handler for different method", () => {
      const handler = vi.fn();
      onNotification("workspace.snapshot", handler);

      const msg = {
        jsonrpc: "2.0",
        method: "turn.started",
        params: {},
      };
      mockSocket.onmessage({ data: JSON.stringify(msg) });

      expect(handler).not.toHaveBeenCalled();
    });
  });

  describe("onRequest", () => {
    it("calls registered handler and sends response back", async () => {
      const handler = vi.fn().mockResolvedValue({ value: "yes" });
      onRequest("ui.request", handler);

      const msg = {
        jsonrpc: "2.0",
        id: 42,
        method: "ui.request",
        params: { prompt: "Allow?" },
      };
      mockSocket.onmessage({ data: JSON.stringify(msg) });

      await vi.waitFor(() => {
        expect(handler).toHaveBeenCalledWith({ prompt: "Allow?" });
      });

      await vi.waitFor(() => {
        expect(sentMessages).toHaveLength(1);
      });

      expect(sentMessages[0]).toEqual({
        jsonrpc: "2.0",
        id: 42,
        result: { value: "yes" },
      });
    });
  });

  describe("message routing", () => {
    it("handles both notification and request in same onmessage", () => {
      const notifHandler = vi.fn();
      const reqHandler = vi.fn().mockResolvedValue("ok");
      onNotification("item.started", notifHandler);
      onRequest("ui.request", reqHandler);

      const notif = {
        jsonrpc: "2.0",
        method: "item.started",
        params: { item_id: "x" },
      };
      mockSocket.onmessage({ data: JSON.stringify(notif) });
      expect(notifHandler).toHaveBeenCalledWith({ item_id: "x" });

      const req = {
        jsonrpc: "2.0",
        id: 99,
        method: "ui.request",
        params: {},
      };
      mockSocket.onmessage({ data: JSON.stringify(req) });
      expect(reqHandler).toHaveBeenCalled();
    });

    it("ignores non-JSON messages", () => {
      expect(() => {
        mockSocket.onmessage({ data: "not json" });
      }).not.toThrow();
    });
  });
});


describe("rpc worker transport", () => {
  it("uses a worker-owned websocket while routing messages on the main thread", () => {
    class FakeWorker {
      sent = [];
      listeners = new Map();
      terminate = vi.fn();

      postMessage(message) {
        this.sent.push(message);
      }

      addEventListener(type, handler) {
        this.listeners.set(type, handler);
      }

      emit(message) {
        this.listeners.get("message")({ data: message });
      }
    }

    const worker = new FakeWorker();
    const transport = createWorkerSocket("ws://localhost:1234", () => worker);
    _setSocket(transport);

    expect(worker.sent[0]).toEqual({ type: "connect", url: "ws://localhost:1234" });
    expect(isRpcConnected()).toBe(false);

    worker.emit({ type: "open" });
    expect(isRpcConnected()).toBe(true);

    const handler = vi.fn();
    onNotification("workspace.snapshot", handler);
    worker.emit({
      type: "message",
      data: JSON.stringify({ jsonrpc: "2.0", method: "workspace.snapshot", params: { ok: true } }),
    });
    expect(handler).toHaveBeenCalledWith({ ok: true });

    rpcNotify("refresh.requested", { reason: "test" });
    const outbound = worker.sent.find((message) => message.type === "send");
    expect(JSON.parse(outbound.data)).toEqual({
      jsonrpc: "2.0",
      method: "refresh.requested",
      params: { reason: "test" },
    });
  });
});
