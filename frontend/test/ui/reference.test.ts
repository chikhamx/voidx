// @ts-nocheck
import { describe, it, expect, vi } from "vitest";
import {
  findRefToken,
  fileInsertionText,
  skillInsertionText,
  refInsertionText,
  renderRefMenu,
} from "../../src/ui/reference";

describe("findRefToken", () => {
  it("returns null when there is no trigger", () => {
    expect(findRefToken("hello world", 5)).toBeNull();
  });

  it("detects @ at the start of text", () => {
    expect(findRefToken("@mai", 4)).toEqual({
      trigger: "@",
      start: 0,
      end: 4,
      query: "mai",
      quoted: false,
    });
  });

  it("detects @ after whitespace", () => {
    const text = "look at @src/vo";
    expect(findRefToken(text, text.length)).toEqual({
      trigger: "@",
      start: 8,
      end: text.length,
      query: "src/vo",
      quoted: false,
    });
  });

  it("ignores @ inside a word", () => {
    expect(findRefToken("user@host", 9)).toBeNull();
  });

  it("returns null when the @ token contains whitespace", () => {
    expect(findRefToken("@foo bar", 8)).toBeNull();
  });

  it("supports quoted @\" tokens with spaces", () => {
    const text = '@"my fi';
    expect(findRefToken(text, text.length)).toEqual({
      trigger: "@",
      start: 0,
      end: text.length,
      query: "my fi",
      quoted: true,
    });
  });

  it("returns null when a quoted token is already closed", () => {
    const text = '@"my file.txt" extra';
    expect(findRefToken(text, text.length)).toBeNull();
  });

  it("detects # skill tokens", () => {
    const text = "run #doc";
    expect(findRefToken(text, text.length)).toEqual({
      trigger: "#",
      start: 4,
      end: text.length,
      query: "doc",
      quoted: false,
    });
  });

  it("ignores ## double hash", () => {
    expect(findRefToken("##hea", 5)).toBeNull();
  });

  it("returns null when the # token contains whitespace", () => {
    expect(findRefToken("#foo bar", 8)).toBeNull();
  });

  it("prefers the nearest valid trigger before the cursor", () => {
    const text = "@foo #bar";
    expect(findRefToken(text, text.length)?.trigger).toBe("#");
    expect(findRefToken(text, 4)?.trigger).toBe("@");
  });

  it("respects the cursor position", () => {
    const text = "@abc @def";
    const token = findRefToken(text, 4);
    expect(token?.query).toBe("abc");
  });
});

describe("fileInsertionText", () => {
  it("inserts dirs without trailing space to allow drilling down", () => {
    expect(fileInsertionText({ rel_path: "src/", kind: "dir", size: 0 })).toBe("@src/");
  });

  it("appends a space after files", () => {
    expect(fileInsertionText({ rel_path: "main.py", kind: "file", size: 3 })).toBe("@main.py ");
  });

  it("quotes paths containing whitespace", () => {
    expect(fileInsertionText({ rel_path: "my file.txt", kind: "file", size: 3 })).toBe('@"my file.txt" ');
  });
});

describe("skillInsertionText", () => {
  it("inserts $name with a trailing space", () => {
    expect(skillInsertionText({ name: "docs", scope: "project", description: "", mode: "manual" })).toBe("$docs ");
  });
});

describe("refInsertionText", () => {
  it("dispatches by candidate type", () => {
    expect(refInsertionText({ type: "file", file: { rel_path: "a.ts", kind: "file", size: 1 } })).toBe("@a.ts ");
    expect(refInsertionText({ type: "skill", skill: { name: "docs", scope: "project", description: "", mode: "manual" } })).toBe("$docs ");
  });
});

describe("renderRefMenu", () => {
  const files = [
    { type: "file", file: { rel_path: "src/", kind: "dir", size: 0 } },
    { type: "file", file: { rel_path: "main.py", kind: "file", size: 10 } },
  ];
  const skills = [
    { type: "skill", skill: { name: "docs", scope: "project", description: "Write docs", mode: "manual" } },
  ];

  it("renders file candidates with kind meta", () => {
    const menu = renderRefMenu(files, 1, () => {});
    const items = menu.querySelectorAll(".ref-item");
    expect(items).toHaveLength(2);
    expect(items[0].querySelector(".ref-name").textContent).toBe("src/");
    expect(items[0].querySelector(".ref-meta").textContent).toBe("dir");
    expect(items[1].classList.contains("selected")).toBe(true);
  });

  it("renders skill candidates with scope and description", () => {
    const menu = renderRefMenu(skills, 0, () => {});
    const item = menu.querySelector(".ref-item");
    expect(item.querySelector(".ref-name").textContent).toBe("#docs");
    expect(item.querySelector(".ref-meta").textContent).toBe("project");
    expect(item.querySelector(".ref-desc").textContent).toBe("Write docs");
  });

  it("invokes onSelect with the clicked candidate", () => {
    const onSelect = vi.fn();
    const menu = renderRefMenu(files, 0, onSelect);
    menu.querySelectorAll(".ref-item")[1].dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(onSelect).toHaveBeenCalledWith(files[1]);
  });

  it("renders an empty menu when there are no candidates", () => {
    const menu = renderRefMenu([], 0, () => {});
    expect(menu.querySelectorAll(".ref-item")).toHaveLength(0);
  });
});

// ── composer wiring (main.ts) ──────────────────────────────────────────
import { beforeEach } from "vitest";
import { _setSocket, _resetForTest as resetRpc } from "../../src/rpc";
import { _resetWorkbenchForTest } from "../../src/main";
import { initDock, _resetForTest as resetDock } from "../../src/ui/dock";
import { initPermissionControls } from "../../src/ui/model";

function setupOpenSocket() {
  const sentMessages: string[] = [];
  const socket = {
    readyState: WebSocket.OPEN,
    onmessage: null as null | ((ev: MessageEvent) => void),
    send: (message: string) => sentMessages.push(message),
  };
  _setSocket(socket as unknown as WebSocket);
  return { sentMessages, socket };
}

function sentPayloads(sentMessages: string[]) {
  return sentMessages.map((raw) => JSON.parse(raw));
}

function typeInput(value: string) {
  const input = document.querySelector<HTMLTextAreaElement>("#input")!;
  input.value = value;
  input.setSelectionRange(value.length, value.length);
  input.dispatchEvent(new Event("input", { bubbles: true }));
  return input;
}

async function waitForRequest(sentMessages: string[], method: string) {
  let request: any;
  await vi.waitFor(() => {
    request = sentPayloads(sentMessages).find((m) => m.method === method);
    expect(request).toBeTruthy();
  });
  return request;
}

describe("reference menu wiring", () => {
  beforeEach(() => {
    resetRpc();
    resetDock();
    _resetWorkbenchForTest();
    initDock();
    initPermissionControls();
  });

  it("requests attachment candidates when typing @", async () => {
    const { sentMessages } = setupOpenSocket();
    typeInput("@ma");
    const request = await waitForRequest(sentMessages, "attachments.candidates");
    expect(request.params.query).toBe("ma");
  });

  it("requests skill candidates when typing #", async () => {
    const { sentMessages } = setupOpenSocket();
    typeInput("#doc");
    const request = await waitForRequest(sentMessages, "skills.candidates");
    expect(request.params.query).toBe("doc");
  });

  it("does not query candidates without a trigger token", async () => {
    const { sentMessages } = setupOpenSocket();
    typeInput("hello world");
    await new Promise((resolve) => setTimeout(resolve, 250));
    expect(sentMessages).toHaveLength(0);
  });

  it("shows candidates from the response and accepts one with Enter", async () => {
    const { sentMessages, socket } = setupOpenSocket();
    const input = typeInput("@ma");
    const request = await waitForRequest(sentMessages, "attachments.candidates");
    socket.onmessage?.({
      data: JSON.stringify({
        jsonrpc: "2.0",
        id: request.id,
        result: { candidates: [{ rel_path: "main.py", kind: "file", size: 3 }] },
      }),
    } as MessageEvent);

    const menu = document.querySelector<HTMLElement>("#ref-menu")!;
    await vi.waitFor(() => {
      expect(menu.classList.contains("visible")).toBe(true);
    });
    expect(menu.textContent).toContain("main.py");

    input.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }),
    );
    expect(input.value).toBe("@main.py ");
    expect(menu.classList.contains("visible")).toBe(false);
  });

  it("inserts $name when accepting a skill candidate", async () => {
    const { sentMessages, socket } = setupOpenSocket();
    const input = typeInput("#doc");
    const request = await waitForRequest(sentMessages, "skills.candidates");
    const mcpRequest = await waitForRequest(sentMessages, "mcp.candidates");
    socket.onmessage?.({
      data: JSON.stringify({
        jsonrpc: "2.0",
        id: mcpRequest.id,
        result: { candidates: [] },
      }),
    } as MessageEvent);
    socket.onmessage?.({
      data: JSON.stringify({
        jsonrpc: "2.0",
        id: request.id,
        result: {
          candidates: [{ name: "docs", scope: "project", description: "Write docs", mode: "manual" }],
        },
      }),
    } as MessageEvent);

    const menu = document.querySelector<HTMLElement>("#ref-menu")!;
    await vi.waitFor(() => {
      expect(menu.classList.contains("visible")).toBe(true);
    });

    input.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true }),
    );
    expect(input.value).toBe("$docs ");
  });

  it("keeps drilling down after accepting a directory candidate", async () => {
    const { sentMessages, socket } = setupOpenSocket();
    const input = typeInput("@s");
    const request = await waitForRequest(sentMessages, "attachments.candidates");
    socket.onmessage?.({
      data: JSON.stringify({
        jsonrpc: "2.0",
        id: request.id,
        result: { candidates: [{ rel_path: "src/", kind: "dir", size: 0 }] },
      }),
    } as MessageEvent);

    const menu = document.querySelector<HTMLElement>("#ref-menu")!;
    await vi.waitFor(() => {
      expect(menu.classList.contains("visible")).toBe(true);
    });

    input.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }),
    );
    expect(input.value).toBe("@src/");
    // Accepting a directory triggers a follow-up query for its children.
    let followUp: any;
    await vi.waitFor(() => {
      const requests = sentPayloads(sentMessages).filter(
        (m) => m.method === "attachments.candidates",
      );
      expect(requests.length).toBeGreaterThan(1);
      followUp = requests[requests.length - 1];
    });
    expect(followUp.params.query).toBe("src/");
  });
});
