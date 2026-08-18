// @ts-nocheck
import { beforeEach, describe, expect, it } from "vitest";
import { createCustomSelect } from "../../src/ui/custom-select";

function mount(overrides = {}) {
  const el = createCustomSelect({
    name: "test_field",
    value: "medium",
    options: ["low", "medium", "high"],
    ...overrides,
  });
  document.body.appendChild(el);
  return el;
}

function hiddenInput(el) {
  return el.querySelector('input[type="hidden"][name="test_field"]');
}

function trigger(el) {
  return el.querySelector(".vx-cselect-trigger");
}

function dropdown(el) {
  return el.querySelector(".vx-cselect-dropdown");
}

beforeEach(() => {
  document.body.innerHTML = "";
});

describe("createCustomSelect", () => {
  it("renders a trigger with the current value and a hidden form input", () => {
    const el = mount();

    expect(trigger(el)).not.toBeNull();
    expect(el.querySelector(".vx-cselect-value").textContent).toBe("medium");
    expect(hiddenInput(el).value).toBe("medium");
    expect(dropdown(el).hidden).toBe(true);
  });

  it("opens the dropdown on trigger click and lists every option", () => {
    const el = mount();

    trigger(el).click();

    expect(dropdown(el).hidden).toBe(false);
    const options = [...el.querySelectorAll(".vx-cselect-option")];
    expect(options.map((o) => o.textContent.replace("✓", "").trim())).toEqual(["low", "medium", "high"]);
  });

  it("syncs selection to the hidden input, fires change, and closes", () => {
    const el = mount();
    const events = [];
    hiddenInput(el).addEventListener("change", () => events.push("change"));

    trigger(el).click();
    [...el.querySelectorAll(".vx-cselect-option")].find((o) => o.dataset.value === "high").click();

    expect(hiddenInput(el).value).toBe("high");
    expect(el.querySelector(".vx-cselect-value").textContent).toBe("high");
    expect(events).toEqual(["change"]);
    expect(dropdown(el).hidden).toBe(true);
  });

  it("marks the selected option as active", () => {
    const el = mount();

    trigger(el).click();

    const active = el.querySelector(".vx-cselect-option.active");
    expect(active).not.toBeNull();
    expect(active.dataset.value).toBe("medium");
  });

  it("closes on Escape", () => {
    const el = mount();
    trigger(el).click();
    expect(dropdown(el).hidden).toBe(false);

    trigger(el).dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));

    expect(dropdown(el).hidden).toBe(true);
  });

  it("closes when clicking outside the component", () => {
    const el = mount();
    trigger(el).click();
    expect(dropdown(el).hidden).toBe(false);

    document.body.dispatchEvent(new MouseEvent("click", { bubbles: true }));

    expect(dropdown(el).hidden).toBe(true);
  });

  it("keeps the dropdown open when clicking inside it", () => {
    const el = mount();
    trigger(el).click();

    dropdown(el).dispatchEvent(new MouseEvent("click", { bubbles: true }));

    expect(dropdown(el).hidden).toBe(false);
  });

  it("supports keyboard navigation: ArrowDown opens, arrows move, Enter selects", () => {
    const el = mount();
    const t = trigger(el);

    t.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
    expect(dropdown(el).hidden).toBe(false);

    t.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
    t.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));

    expect(hiddenInput(el).value).toBe("low");
    expect(dropdown(el).hidden).toBe(true);
  });

  it("moves keyboard highlight upward without selecting", () => {
    const el = mount();
    const t = trigger(el);

    t.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
    t.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
    t.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowUp", bubbles: true }));
    t.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));

    expect(hiddenInput(el).value).toBe("medium");
  });

  it("uses optionLabel for display while keeping raw values in the input", () => {
    const labels = { system: "跟随系统", light: "浅色", dark: "深色" };
    const el = mount({
      name: "test_field",
      value: "light",
      options: ["system", "light", "dark"],
      optionLabel: (v) => labels[v] ?? v,
    });

    expect(el.querySelector(".vx-cselect-value").textContent).toBe("浅色");
    trigger(el).click();
    [...el.querySelectorAll(".vx-cselect-option")].find((o) => o.dataset.value === "dark").click();

    expect(hiddenInput(el).value).toBe("dark");
    expect(el.querySelector(".vx-cselect-value").textContent).toBe("深色");
  });

  it("renders an empty-string option with a none label", () => {
    const el = mount({ name: "test_field", value: "", options: ["", "alpha"] });

    trigger(el).click();

    const first = el.querySelector('.vx-cselect-option[data-value=""]');
    expect(first).not.toBeNull();
    expect(first.textContent.replace("✓", "").trim()).toBe("none");
  });

  it("reflects external value writes to the hidden input", () => {
    const el = mount();

    hiddenInput(el).value = "high";
    hiddenInput(el).dispatchEvent(new Event("input", { bubbles: true }));

    expect(el.querySelector(".vx-cselect-value").textContent).toBe("high");
  });
});
