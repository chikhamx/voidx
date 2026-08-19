import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  _resetNavigationForTest,
  initThreadNavigation,
  recordThreadVisit,
} from "../../src/ui/navigation";

function resetNavigationButtons(): void {
  const left = document.querySelector<HTMLElement>(".vx-titlebar-left");
  if (!left) throw new Error("titlebar left is missing");
  left.querySelectorAll("#titlebar-sidebar-toggle, #titlebar-history-back, #titlebar-history-forward")
    .forEach((button) => button.remove());
  for (const [id, label] of [
    ["titlebar-history-back", "后退"],
    ["titlebar-history-forward", "前进"],
  ] as const) {
    const button = document.createElement("button");
    button.id = id;
    button.type = "button";
    button.setAttribute("aria-label", label);
    left.append(button);
  }
}

async function settleNavigation(): Promise<void> {
  await new Promise<void>((resolve) => setTimeout(resolve, 0));
}

beforeEach(() => {
  _resetNavigationForTest();
  resetNavigationButtons();
});

describe("titlebar thread navigation", () => {
  it("navigates through visited threads and updates button state", async () => {
    const navigate = vi.fn().mockResolvedValue(undefined);
    initThreadNavigation(navigate);
    recordThreadVisit("thread-a");
    recordThreadVisit("thread-b");

    const back = document.querySelector<HTMLButtonElement>("#titlebar-history-back")!;
    const forward = document.querySelector<HTMLButtonElement>("#titlebar-history-forward")!;
    expect(back.disabled).toBe(false);
    expect(forward.disabled).toBe(true);

    back.click();
    await settleNavigation();
    expect(navigate).toHaveBeenLastCalledWith("thread-a");
    expect(back.disabled).toBe(true);
    expect(forward.disabled).toBe(false);

    forward.click();
    await settleNavigation();
    expect(navigate).toHaveBeenLastCalledWith("thread-b");
    expect(back.disabled).toBe(false);
    expect(forward.disabled).toBe(true);
  });

  it("restores the previous history position when navigation fails", async () => {
    const navigate = vi.fn()
      .mockResolvedValueOnce(undefined)
      .mockRejectedValueOnce(new Error("switch failed"));
    initThreadNavigation(navigate);
    recordThreadVisit("thread-a");
    recordThreadVisit("thread-b");

    document.querySelector<HTMLButtonElement>("#titlebar-history-back")!.click();
    await settleNavigation();
    expect(document.querySelector<HTMLButtonElement>("#titlebar-history-forward")!.disabled).toBe(false);

    document.querySelector<HTMLButtonElement>("#titlebar-history-forward")!.click();
    await settleNavigation();
    expect(document.querySelector<HTMLButtonElement>("#titlebar-history-back")!.disabled).toBe(true);
    expect(document.querySelector<HTMLButtonElement>("#titlebar-history-forward")!.disabled).toBe(false);
  });
});
