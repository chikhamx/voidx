/**
 * Custom select — 现代简洁的自定义下拉，替代原生 <select>。
 * 内部维护同名 hidden input，选中变化时派发 change，表单收集逻辑零改动兼容。
 */

export interface CustomSelectConfig {
  name: string;
  value: string;
  options: string[];
  optionLabel?: (value: string) => string;
}

const CHEVRON_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>';

const CHECK_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>';

export function createCustomSelect(config: CustomSelectConfig): HTMLElement {
  const root = document.createElement("div");
  root.className = "vx-cselect";

  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "vx-cselect-trigger";
  trigger.setAttribute("aria-haspopup", "listbox");
  trigger.setAttribute("aria-expanded", "false");

  const valueEl = document.createElement("span");
  valueEl.className = "vx-cselect-value";

  const chevron = document.createElement("span");
  chevron.className = "vx-cselect-chevron";
  chevron.innerHTML = CHEVRON_SVG;

  trigger.append(valueEl, chevron);

  const dropdown = document.createElement("div");
  dropdown.className = "vx-cselect-dropdown";
  dropdown.setAttribute("role", "listbox");
  dropdown.hidden = true;

  const input = document.createElement("input");
  input.type = "hidden";
  input.name = config.name;

  let currentValue = config.value;
  let highlightIndex = -1;
  let isOpen = false;

  const labelFor = (v: string): string =>
    config.optionLabel ? config.optionLabel(v) : v || "none";

  const optionEls: HTMLButtonElement[] = config.options.map((optionValue) => {
    const option = document.createElement("button");
    option.type = "button";
    option.className = "vx-cselect-option";
    option.dataset.value = optionValue;
    option.setAttribute("role", "option");
    option.tabIndex = -1;

    const label = document.createElement("span");
    label.className = "vx-cselect-option-label";
    label.textContent = labelFor(optionValue);

    const check = document.createElement("span");
    check.className = "vx-cselect-option-check";
    check.innerHTML = CHECK_SVG;

    option.append(label, check);
    option.addEventListener("click", () => selectValue(optionValue));
    dropdown.append(option);
    return option;
  });

  function syncUI(): void {
    valueEl.textContent = labelFor(currentValue);
    if (input.value !== currentValue) input.value = currentValue;
    optionEls.forEach((el, i) => {
      el.classList.toggle("active", config.options[i] === currentValue);
      el.setAttribute("aria-selected", config.options[i] === currentValue ? "true" : "false");
    });
  }

  function syncHighlight(): void {
    optionEls.forEach((el, i) => el.classList.toggle("highlight", i === highlightIndex));
  }

  function onDocumentClick(e: Event): void {
    if (!root.contains(e.target as Node)) close();
  }

  function open(): void {
    if (isOpen) return;
    isOpen = true;
    highlightIndex = -1;
    dropdown.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
    root.classList.add("open");
    dropdown.classList.remove("vx-cselect-dropup");
    const rect = trigger.getBoundingClientRect();
    const spaceBelow = window.innerHeight - rect.bottom;
    if (spaceBelow < 240 && rect.top > spaceBelow) {
      dropdown.classList.add("vx-cselect-dropup");
    }
    document.addEventListener("click", onDocumentClick);
  }

  function close(): void {
    if (!isOpen) return;
    isOpen = false;
    dropdown.hidden = true;
    trigger.setAttribute("aria-expanded", "false");
    root.classList.remove("open");
    document.removeEventListener("click", onDocumentClick);
  }

  function selectValue(v: string): void {
    currentValue = v;
    syncUI();
    input.dispatchEvent(new Event("change", { bubbles: true }));
    close();
    trigger.focus();
  }

  trigger.addEventListener("click", () => (isOpen ? close() : open()));

  root.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (isOpen) {
        e.preventDefault();
        close();
        trigger.focus();
      }
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (!isOpen) open();
      else {
        highlightIndex = Math.min(highlightIndex + 1, config.options.length - 1);
        syncHighlight();
      }
      return;
    }
    if (e.key === "ArrowUp") {
      if (isOpen) {
        e.preventDefault();
        highlightIndex = Math.max(highlightIndex - 1, -1);
        syncHighlight();
      }
      return;
    }
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      if (!isOpen) open();
      else if (highlightIndex >= 0) selectValue(config.options[highlightIndex]);
      else close();
    }
  });

  // 外部直接改写 hidden input 的值时（如程序化设值），同步显示
  input.addEventListener("input", () => {
    if (input.value !== currentValue) {
      currentValue = input.value;
      syncUI();
    }
  });

  root.append(trigger, dropdown, input);
  syncUI();
  return root;
}
