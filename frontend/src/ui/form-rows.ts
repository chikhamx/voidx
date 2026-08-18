/**
 * Form rows — settings 风格表单行的共享 DOM 构造器。
 * 供 settings / providers 等弹窗复用，样式类见 components.css。
 */

export function section(title: string, children: HTMLElement[]): HTMLElement {
  const el = document.createElement("section");
  el.className = "settings-section";
  const heading = document.createElement("h3");
  heading.textContent = title;
  el.append(heading, ...children);
  return el;
}

export function rowBase(label: string): HTMLLabelElement {
  const row = document.createElement("label");
  row.className = "settings-row";
  const text = document.createElement("span");
  text.textContent = label;
  row.append(text);
  return row;
}

export function inputRow(label: string, name: string, value: string): HTMLLabelElement {
  const row = rowBase(label);
  const input = document.createElement("input");
  input.name = name;
  input.value = value;
  row.append(input);
  return row;
}

export function secretRow(label: string, name: string, value: string): HTMLLabelElement {
  const row = rowBase(label);
  const input = document.createElement("input");
  input.type = "password";
  input.name = name;
  input.value = value;
  input.autocomplete = "off";
  row.append(input);
  return row;
}

export function numberRow(label: string, name: string, value: number, min: number, max: number): HTMLLabelElement {
  const row = rowBase(label);
  const input = document.createElement("input");
  input.type = "number";
  input.name = name;
  input.value = String(value);
  input.min = String(min);
  input.max = String(max);
  row.append(input);
  return row;
}
