// @ts-nocheck
import { describe, it, expect, beforeEach } from "vitest";
import { renderTextRequest } from "../../src/ui/dialog";

const controlsEl = document.querySelector("#request-controls");

beforeEach(() => {
  controlsEl.replaceChildren();
});

describe("renderTextRequest", () => {
  it("uses a textarea for plain text requests", () => {
    renderTextRequest({ request_id: "r1", prompt: "Name?", default: "x" });
    const field = controlsEl.querySelector("textarea");
    expect(field).not.toBeNull();
    expect(field.value).toBe("x");
    expect(controlsEl.querySelector("input[type=password]")).toBeNull();
  });

  it("masks secret requests with a password input", () => {
    renderTextRequest({ request_id: "r2", prompt: "API key?", secret: true });
    const field = controlsEl.querySelector("input[type=password]");
    expect(field).not.toBeNull();
    expect(controlsEl.querySelector("textarea")).toBeNull();
  });

  it("keeps the default value in the masked field", () => {
    renderTextRequest({ request_id: "r3", prompt: "Token?", secret: true, default: "sk-1" });
    const field = controlsEl.querySelector("input[type=password]");
    expect(field.value).toBe("sk-1");
  });
});
