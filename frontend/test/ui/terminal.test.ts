// @ts-nocheck
import { beforeEach, describe, it, expect, vi } from "vitest";
import {
  initTerminal,
  appendTerminalOutput,
  showTerminalClosed,
  onTerminalInput,
  onTerminalStart,
    onTerminalResize,
    onTerminalStop,
    terminateActiveTerminal,
    setActiveTerminal,
    _resetForTest,
    _triggerTerminalResizeForTest,
} from "../../src/ui/terminal";

beforeEach(() => {
  _resetForTest();
  const pane = document.querySelector("#terminal-pane");
  if (pane) pane.innerHTML = "";
});

describe("initTerminal", () => {
  it("creates start button when no terminal is active", () => {
    initTerminal();
    const pane = document.querySelector("#terminal-pane");
    const btn = pane.querySelector(".vx-terminal-start");
    expect(btn).not.toBeNull();
    expect(btn.textContent).toContain("Start");
  });

  it("calls onTerminalStart callback when start button clicked", () => {
    const cb = vi.fn();
    onTerminalStart(cb);
    initTerminal();

    const pane = document.querySelector("#terminal-pane");
    const btn = pane.querySelector(".vx-terminal-start");
    btn.click();

    expect(cb).toHaveBeenCalled();
  });
});

describe("appendTerminalOutput", () => {
  it("appends output text to terminal pane", () => {
    initTerminal();
    appendTerminalOutput("t1", "hello world");

    const pane = document.querySelector("#terminal-pane");
    expect(pane.textContent).toContain("hello world");
  });

  it("accumulates output from multiple calls", () => {
    initTerminal();
    appendTerminalOutput("t1", "line 1\n");
    appendTerminalOutput("t1", "line 2\n");

    const pane = document.querySelector("#terminal-pane");
    expect(pane.textContent).toContain("line 1");
    expect(pane.textContent).toContain("line 2");
  });

  it("handles different terminal IDs separately", () => {
    initTerminal();
    appendTerminalOutput("t1", "terminal 1 output");
    appendTerminalOutput("t2", "terminal 2 output");

    const pane = document.querySelector("#terminal-pane");
    expect(pane.textContent).toContain("terminal 1 output");
    expect(pane.textContent).toContain("terminal 2 output");
  });
});

describe("showTerminalClosed", () => {
  it("shows closed status for terminal", () => {
    initTerminal();
    appendTerminalOutput("t1", "some output");
    showTerminalClosed("t1");

    const pane = document.querySelector("#terminal-pane");
    expect(pane.textContent).toContain("closed");
  });
});

describe("onTerminalInput", () => {
  it("calls callback when user types in terminal input", () => {
    const cb = vi.fn();
    onTerminalInput(cb);
    initTerminal();
    appendTerminalOutput("t1", "output");

    const input = document.querySelector("#terminal-pane .vx-terminal-input");
    input.value = "ls -la";
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));

    expect(cb).toHaveBeenCalledWith("t1", "ls -la");
  });
});

describe("onTerminalResize", () => {
    it("debounces terminal resize calls by 100ms", () => {
        vi.useFakeTimers();
        try {
            const cb = vi.fn();
            onTerminalResize(cb);
            setActiveTerminal("t1");

            _triggerTerminalResizeForTest(80, 24);
            _triggerTerminalResizeForTest(100, 30);
            expect(cb).not.toHaveBeenCalled();

            vi.advanceTimersByTime(100);
            expect(cb).toHaveBeenCalledTimes(1);
            expect(cb).toHaveBeenCalledWith("t1", 100, 30);
        } finally {
            vi.useRealTimers();
        }
    });

    it("does not trigger resize callback when activeTerminalId is not set", () => {
        vi.useFakeTimers();
        try {
            const cb = vi.fn();
            onTerminalResize(cb);

            _triggerTerminalResizeForTest(80, 24);
            vi.advanceTimersByTime(100);
            expect(cb).not.toHaveBeenCalled();
        } finally {
            vi.useRealTimers();
        }
    });
});

describe("terminateActiveTerminal", () => {
    it("invokes stop callback and shows terminal closed", () => {
        const cb = vi.fn();
        onTerminalStop(cb);
        initTerminal();
        appendTerminalOutput("t1", "shell output");

        terminateActiveTerminal();

        expect(cb).toHaveBeenCalledWith("t1");
        const pane = document.querySelector("#terminal-pane");
        expect(pane.textContent).toContain("closed");
    });

    it("is a no-op when activeTerminalId is null", () => {
        const cb = vi.fn();
        onTerminalStop(cb);
        terminateActiveTerminal();
        expect(cb).not.toHaveBeenCalled();
    });
});
