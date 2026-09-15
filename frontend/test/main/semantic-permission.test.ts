import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import fixture from "../fixtures/semantic-permission.json";
import { _resetWorkbenchForTest, handleNotification } from "../../src/main";
import { _resetForTest as resetRpc, _setSocket, _resolvePendingForTest } from "../../src/rpc/client";
import { uiState, requestDialogEl } from "../../src/services/state";
import { _resetDialogForTest, pendingUiRequests } from "../../src/ui/dialog";

const deliver = (messages: Array<{ method: string; params: unknown }>) => {
    for (const message of messages) {
        handleNotification(message.method, structuredClone(message.params) as Record<string, unknown>);
    }
};

let socket: { close: () => void; readyState: number; send: ReturnType<typeof vi.fn<(data: string) => void>>; addEventListener: (type: string, listener: EventListener) => void; dispatchEvent: (event: Event) => boolean };

beforeEach(() => {
    resetRpc();
    _resetWorkbenchForTest();
    _resetDialogForTest();
    uiState.sessionId = "thread-1";
    uiState.isSwitchingThread = false;
    const events = new EventTarget();
    socket = { close: () => { events.dispatchEvent(new Event("close")); }, dispatchEvent: events.dispatchEvent.bind(events), readyState: WebSocket.OPEN, send: vi.fn(), addEventListener: events.addEventListener.bind(events) };
    _setSocket(socket);
});

afterEach(() => {
    _resetDialogForTest();
    resetRpc();
    vi.restoreAllMocks();
});

describe("real SDK Gateway permission fixture", () => {
    it("presents once, deduplicates pending reconnect and clears on actual resolved completion", () => {
        expect(fixture.live.map((message) => message.method)).toEqual(["ui.request", "item.completed"]);
        expect(fixture.pending_reconnect.map(m => m.method)).toEqual(["workspace.snapshot", "ui.request"]);
        expect(fixture.live[1].params).toMatchObject({
            thread_id: "thread-1", turn_id: "turn-1", item_id: "item-1", kind: "prompt",
            lifecycle: "completed", data: { prompt_type: "permission", request_id: "request-1", cleared: true },
        });
        const show = vi.spyOn(requestDialogEl, "showModal");
        deliver(fixture.live.slice(0, 1));
        expect(requestDialogEl.open).toBe(true);
        expect(requestDialogEl.dataset.requestId).toBe("request-1");
        expect(document.querySelector("#request-title")?.textContent).toBe("权限审批");
        expect(document.querySelector("#request-details")?.textContent).toContain("Allow tools: write?");
        expect(document.querySelectorAll("#request-controls .request-choice")).toHaveLength(3);
        expect(pendingUiRequests).toEqual([]);
        socket.close();
        expect(requestDialogEl.open).toBe(false);
        _setSocket(socket);
        deliver(fixture.pending_reconnect);
        deliver(fixture.pending_reconnect);
        expect(show).toHaveBeenCalledTimes(2);
        expect(pendingUiRequests).toEqual([]);
        deliver(fixture.live.slice(1));
        expect(requestDialogEl.open).toBe(false);
        expect(pendingUiRequests).toEqual([]);
        expect(socket.send.mock.calls.map(([text]) => JSON.parse(text).method))
            .not.toContain("session.respond");
        // Simulate this client missing the resolution delivered to another client.
        deliver(fixture.live.slice(0, 1));
        socket.close();
        expect(requestDialogEl.open).toBe(false);
        _setSocket(socket);
        expect(fixture.fresh_reconnect.map(m => m.method)).toEqual(["workspace.snapshot"]);
        deliver(fixture.fresh_reconnect);
        expect(document.body.textContent).toContain("File created");
        expect(requestDialogEl.open).toBe(false);
        expect(pendingUiRequests).toEqual([]);
    });

    it("sends the selected permission through session.respond with exact ownership", async () => {
        deliver(fixture.live.slice(0, 1));
        const buttons = document.querySelectorAll<HTMLButtonElement>("#request-controls button");
        buttons[1].click();
        expect(socket.send).toHaveBeenCalledTimes(1);
        expect(JSON.parse(socket.send.mock.calls[0][0])).toEqual({
            jsonrpc: "2.0", id: 1, method: "session.respond",
            params: { request_id: "request-1", thread_id: "thread-1", value: "allow" },
        });
        expect(requestDialogEl.open).toBe(true);
        _resolvePendingForTest(1, { ok: true });
        await Promise.resolve();
        expect(requestDialogEl.open).toBe(false);
        deliver(fixture.live.slice(1));
        expect(requestDialogEl.open).toBe(false);
        expect(pendingUiRequests).toEqual([]);
    });
});

it("cleans real close and socket replacement, ignoring old socket close", () => {
    const req = { ...fixture.live[0].params, reconnect_policy: "replace" };
    handleNotification("ui.request", req);
    socket.close();
    expect(requestDialogEl.open).toBe(false);
    const old = socket;
    _setSocket({ readyState: WebSocket.OPEN, send: vi.fn(), addEventListener() {}, close() {} });
    handleNotification("ui.request", req);
    old.dispatchEvent(new Event("close"));
    expect(requestDialogEl.open).toBe(true);
    _setSocket(null);
    expect(requestDialogEl.open).toBe(false);
});
