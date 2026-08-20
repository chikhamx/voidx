/**
 * Agent Studio — 页面式自定义 Agent 配置视图（非弹窗）。
 * 打开时占据主区域（隐藏聊天元素），关闭时恢复原状。
 * 表单分区渲染自 agent-catalog RPC 返回的元数据；payload 组装见 agent-payload.ts。
 */

import {
  buildAgentProfilePayload,
  type LinearStepState,
  type StudioFormState,
} from "./agent-payload";
import { rowBase, section } from "./form-rows";

export interface AgentCatalogTool {
  id: string;
  description: string;
}

export interface AgentCatalogIntegration {
  name: string;
  description: string;
}

export interface AgentCatalogNode {
  name: string;
  description: string;
}

export interface AgentCatalogEdge {
  source: string;
  target: string;
  condition: string;
  label: string;
}

export interface AgentCatalog {
  tools: AgentCatalogTool[];
  skills: AgentCatalogIntegration[];
  mcp_servers: AgentCatalogIntegration[];
  builtin_nodes: AgentCatalogNode[];
  default_edges: AgentCatalogEdge[];
}

export interface AgentProfileDiagnostic {
  path: string;
  code: string;
  message: string;
  severity?: string;
}

export interface AgentStudioRpc {
  getCatalog: () => Promise<AgentCatalog>;
  validate: (params: {
    scope: string;
    name: string;
    payload: Record<string, unknown>;
  }) => Promise<{ valid: boolean; diagnostics: AgentProfileDiagnostic[] }>;
  save: (params: {
    scope: string;
    name: string;
    payload: Record<string, unknown>;
    expected_revision: number;
  }) => Promise<{ diagnostics?: AgentProfileDiagnostic[] }>;
}

export interface AgentStudioOptions {
  rpc: AgentStudioRpc;
  onSaved: (profileName: string) => void;
}

const CHAT_ELEMENT_IDS = ["empty-state", "transcript", "composer"];

interface StudioState {
  rpc: AgentStudioRpc;
  onSaved: (profileName: string) => void;
  catalog: AgentCatalog;
  priorHidden: Record<string, boolean>;
}

let studioState: StudioState | null = null;

function studioRoot(): HTMLElement | null {
  return document.querySelector<HTMLElement>("#agent-studio");
}

function setChatElementsHidden(hidden: boolean): void {
  for (const id of CHAT_ELEMENT_IDS) {
    const el = document.querySelector<HTMLElement>(`#${id}`);
    if (el) el.hidden = hidden;
  }
}

export async function openAgentStudio(options: AgentStudioOptions): Promise<void> {
  const root = studioRoot();
  if (!root || !root.hidden) return;
  const priorHidden: Record<string, boolean> = {};
  for (const id of CHAT_ELEMENT_IDS) {
    const el = document.querySelector<HTMLElement>(`#${id}`);
    if (el) priorHidden[id] = Boolean(el.hidden);
  }
  let catalog: AgentCatalog;
  try {
    catalog = await options.rpc.getCatalog();
  } catch (error) {
    studioState = {
      rpc: options.rpc,
      onSaved: options.onSaved,
      catalog: { tools: [], skills: [], mcp_servers: [], builtin_nodes: [], default_edges: [] },
      priorHidden,
    };
    root.replaceChildren(renderStudioError(error));
    setChatElementsHidden(true);
    root.hidden = false;
    return;
  }
  studioState = { rpc: options.rpc, onSaved: options.onSaved, catalog, priorHidden };
  root.replaceChildren(renderStudioPage(catalog));
  setChatElementsHidden(true);
  root.hidden = false;
}

function renderStudioError(error: unknown): DocumentFragment {
  const fragment = document.createDocumentFragment();
  const header = document.createElement("header");
  header.className = "vx-agent-studio-header";
  const back = document.createElement("button");
  back.type = "button";
  back.className = "vx-agent-studio-back";
  back.textContent = "← 返回";
  back.addEventListener("click", () => closeAgentStudio());
  const title = document.createElement("span");
  title.className = "vx-agent-studio-title";
  title.textContent = "新建自定义 Agent";
  header.append(back, title);
  const message = document.createElement("p");
  message.className = "vx-agent-studio-error";
  message.textContent = `加载配置元数据失败：${errorMessage(error)}`;
  fragment.append(header, message);
  return fragment;
}

export function closeAgentStudio(): void {
  const root = studioRoot();
  if (root) {
    root.hidden = true;
    root.replaceChildren();
  }
  const prior = studioState?.priorHidden ?? {};
  for (const id of CHAT_ELEMENT_IDS) {
    const el = document.querySelector<HTMLElement>(`#${id}`);
    if (el) el.hidden = prior[id] ?? false;
  }
  studioState = null;
}

function renderStudioPage(catalog: AgentCatalog): DocumentFragment {
  const fragment = document.createDocumentFragment();
  const header = document.createElement("header");
  header.className = "vx-agent-studio-header";
  const back = document.createElement("button");
  back.type = "button";
  back.className = "vx-agent-studio-back";
  back.textContent = "← 返回";
  back.addEventListener("click", () => closeAgentStudio());
  const title = document.createElement("span");
  title.className = "vx-agent-studio-title";
  title.textContent = "新建自定义 Agent";
  header.append(back, title);

  const body = document.createElement("div");
  body.className = "vx-agent-studio-body";
  body.append(
    renderBasicSection(),
    renderPromptSection(),
    renderRunModeSection(),
    renderWorkflowSection(catalog),
    renderResourcesSection(catalog),
  );

  const actionBar = document.createElement("div");
  actionBar.className = "vx-agent-studio-actions";
  const validateButton = document.createElement("button");
  validateButton.type = "button";
  validateButton.id = "studio-validate";
  validateButton.textContent = "校验";
  const saveButton = document.createElement("button");
  saveButton.type = "button";
  saveButton.id = "studio-save";
  saveButton.textContent = "保存";
  const diagnostics = document.createElement("div");
  diagnostics.id = "studio-diagnostics";
  diagnostics.setAttribute("role", "status");
  validateButton.addEventListener("click", () => void runStudioValidate());
  saveButton.addEventListener("click", () => void runStudioSave());
  actionBar.append(validateButton, saveButton, diagnostics);

  fragment.append(header, body, actionBar);
  return fragment;
}

function renderBasicSection(): HTMLElement {
  const nameRow = rowBase("标识名称");
  const nameInput = document.createElement("input");
  nameInput.id = "studio-name";
  nameInput.placeholder = "小写字母/数字/连字符，如 my-reviewer";
  nameRow.append(nameInput);

  const displayRow = rowBase("显示名称");
  const displayInput = document.createElement("input");
  displayInput.id = "studio-display-name";
  displayInput.placeholder = "模式下拉中显示的名称";
  displayRow.append(displayInput);

  const scopeRow = rowBase("保存位置");
  for (const [value, label] of [
    ["project", "项目（.voidx/agents/）"],
    ["global", "全局（~/.voidx/agents/）"],
  ] as const) {
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "studio-scope";
    radio.value = value;
    if (value === "project") radio.checked = true;
    scopeRow.append(radio, document.createTextNode(label));
  }
  return section("基本信息", [nameRow, displayRow, scopeRow]);
}

function renderPromptSection(): HTMLElement {
  const policyRow = rowBase("基础策略");
  const policy = document.createElement("select");
  policy.id = "studio-prompt-policy";
  for (const value of ["coding", "chat", "goal", "loop"]) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    policy.append(option);
  }
  policyRow.append(policy);

  const identityRow = rowBase("身份定义");
  const identity = document.createElement("textarea");
  identity.id = "studio-identity";
  identity.rows = 3;
  identity.placeholder = "这个 Agent 是谁、负责什么";
  identityRow.append(identity);

  const personaRow = rowBase("人格 / 语气");
  const persona = document.createElement("input");
  persona.id = "studio-persona";
  personaRow.append(persona);

  const styleRules = dynamicTextRows("studio-style-rules", "风格规则", "如：回复保持简洁");
  const extraRules = dynamicTextRows("studio-extra-rules", "附加规则", "如：不要改动无关文件");
  const suppressSections = dynamicTextRows("studio-suppress-sections", "屏蔽段落", "基础提示中要屏蔽的段落名");

  return section("Prompt 风格", [
    policyRow,
    identityRow,
    personaRow,
    styleRules,
    extraRules,
    suppressSections,
  ]);
}

function renderRunModeSection(): HTMLElement {
  const runRow = rowBase("运行模式");
  for (const [value, label] of [
    ["single", "single（单次执行）"],
    ["goal_eval", "goal_eval（目标评估）"],
    ["loop_dynamic", "loop_dynamic（动态循环）"],
  ] as const) {
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "studio-run-mode";
    radio.value = value;
    if (value === "single") radio.checked = true;
    runRow.append(radio, document.createTextNode(label));
  }
  const hitlRow = rowBase("审批模式");
  for (const [value, label] of [
    ["interactive", "interactive（逐步确认）"],
    ["autonomous", "autonomous（自动执行）"],
  ] as const) {
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "studio-hitl";
    radio.value = value;
    if (value === "interactive") radio.checked = true;
    hitlRow.append(radio, document.createTextNode(label));
  }
  return section("运行方式", [runRow, hitlRow]);
}

function renderWorkflowSection(catalog: AgentCatalog): HTMLElement {
  const modeRow = rowBase("工作流模式");
  const radios: HTMLInputElement[] = [];
  for (const [value, label] of [
    ["default", "默认（内置完整流程）"],
    ["builtin", "内置节点组合"],
    ["linear", "自定义线性流程"],
  ] as const) {
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "studio-workflow-mode";
    radio.value = value;
    if (value === "default") radio.checked = true;
    radios.push(radio);
    modeRow.append(radio, document.createTextNode(label));
  }
  const builtinPanel = renderBuiltinWorkflowPanel(catalog);
  const linearPanel = renderLinearWorkflowPanel(catalog);
  const syncPanels = () => {
    const mode = radios.find((radio) => radio.checked)?.value ?? "default";
    builtinPanel.hidden = mode !== "builtin";
    linearPanel.hidden = mode !== "linear";
  };
  for (const radio of radios) radio.addEventListener("change", syncPanels);
  return section("工作流", [modeRow, builtinPanel, linearPanel]);
}

function renderBuiltinWorkflowPanel(catalog: AgentCatalog): HTMLElement {
  const panel = document.createElement("div");
  panel.id = "studio-workflow-builtin";
  panel.hidden = true;
  panel.className = "vx-studio-checkbox-group";
  const hint = document.createElement("p");
  hint.className = "vx-studio-hint";
  hint.textContent = "勾选要保留的内置节点，节点间自动按内置流程连线。";
  panel.append(hint);
  for (const node of catalog.builtin_nodes) {
    const label = document.createElement("label");
    label.className = "vx-studio-checkbox";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.value = node.name;
    const text = document.createElement("span");
    text.textContent = `${node.name} — ${node.description}`;
    label.append(box, text);
    panel.append(label);
  }
  return panel;
}

function renderLinearWorkflowPanel(catalog: AgentCatalog): HTMLElement {
  const panel = document.createElement("div");
  panel.id = "studio-workflow-linear";
  panel.hidden = true;
  const hint = document.createElement("p");
  hint.className = "vx-studio-hint";
  hint.textContent = "按顺序定义步骤 1、2、3…，保存时自动串联为线性流程。";
  const list = document.createElement("div");
  list.className = "vx-studio-step-list";
  const add = document.createElement("button");
  add.type = "button";
  add.className = "vx-studio-step-add";
  add.textContent = "＋ 添加步骤";
  add.addEventListener("click", () => {
    list.append(buildLinearStep(catalog));
    renumberLinearSteps(list);
  });
  panel.append(hint, list, add);
  return panel;
}

function buildLinearStep(catalog: AgentCatalog): HTMLElement {
  const step = document.createElement("div");
  step.className = "vx-studio-step";

  const head = document.createElement("div");
  head.className = "vx-studio-step-head";
  const order = document.createElement("span");
  order.className = "vx-studio-step-order";
  const kind = document.createElement("select");
  kind.className = "vx-studio-step-kind";
  for (const [value, label] of [
    ["ref", "引用内置节点"],
    ["custom", "自定义节点"],
  ] as const) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    kind.append(option);
  }
  const ref = document.createElement("select");
  ref.className = "vx-studio-step-ref";
  for (const node of catalog.builtin_nodes) {
    const option = document.createElement("option");
    option.value = node.name;
    option.textContent = `${node.name} — ${node.description}`;
    ref.append(option);
  }
  const up = stepButton("vx-studio-step-up", "↑");
  const down = stepButton("vx-studio-step-down", "↓");
  const remove = stepButton("vx-studio-step-remove", "删除");
  head.append(order, kind, ref, up, down, remove);

  const custom = document.createElement("div");
  custom.className = "vx-studio-step-custom";
  custom.hidden = true;
  custom.append(
    customField("vx-studio-step-custom-name", "节点标识（小写）"),
    customField("vx-studio-step-custom-goal", "目标"),
    customField("vx-studio-step-custom-description", "描述"),
    customField("vx-studio-step-custom-persona", "persona"),
    customArea("vx-studio-step-custom-steps", "步骤说明（每行一条）"),
    customArea("vx-studio-step-custom-rules", "规则（每行一条）"),
  );

  const conditionRow = document.createElement("label");
  conditionRow.className = "vx-studio-step-condition-row";
  conditionRow.append(document.createTextNode("完成条件"));
  const condition = document.createElement("input");
  condition.className = "vx-studio-step-condition";
  condition.value = "completed";
  conditionRow.append(condition);

  kind.addEventListener("change", () => {
    const isCustom = kind.value === "custom";
    custom.hidden = !isCustom;
    ref.hidden = isCustom;
  });
  up.addEventListener("click", () => {
    const prev = step.previousElementSibling;
    if (prev) {
      step.parentElement!.insertBefore(step, prev);
      renumberLinearSteps(step.parentElement!);
    }
  });
  down.addEventListener("click", () => {
    const next = step.nextElementSibling;
    if (next) {
      step.parentElement!.insertBefore(next, step);
      renumberLinearSteps(step.parentElement!);
    }
  });
  remove.addEventListener("click", () => {
    const parent = step.parentElement;
    step.remove();
    if (parent) renumberLinearSteps(parent);
  });

  step.append(head, custom, conditionRow);
  return step;
}

function stepButton(className: string, text: string): HTMLButtonElement {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = text;
  return button;
}

function customArea(className: string, placeholder: string): HTMLTextAreaElement {
  const area = document.createElement("textarea");
  area.className = className;
  area.rows = 2;
  area.placeholder = placeholder;
  return area;
}

function customField(className: string, placeholder: string): HTMLInputElement {
  const input = document.createElement("input");
  input.type = "text";
  input.className = className;
  input.placeholder = placeholder;
  return input;
}

function renumberLinearSteps(list: ParentNode): void {
  [...list.querySelectorAll<HTMLElement>(".vx-studio-step")].forEach((step, index) => {
    const order = step.querySelector<HTMLElement>(".vx-studio-step-order");
    if (order) order.textContent = String(index + 1);
  });
}

function renderResourcesSection(catalog: AgentCatalog): HTMLElement {
  const tools = checkboxList("studio-tools", catalog.tools.map((tool) => ({
    value: tool.id,
    label: tool.id,
    hint: tool.description,
    checked: true,
  })));
  const skills = checkboxList("studio-skills", catalog.skills.map((skill) => ({
    value: skill.name,
    label: skill.name,
    hint: skill.description,
    checked: false,
  })));
  const mcp = checkboxList("studio-mcp", catalog.mcp_servers.map((server) => ({
    value: server.name,
    label: server.name,
    hint: server.description,
    checked: false,
  })));
  return section("工具权限", [tools, skills, mcp]);
}

interface CheckboxItem {
  value: string;
  label: string;
  hint: string;
  checked: boolean;
}

function checkboxList(id: string, items: CheckboxItem[]): HTMLElement {
  const group = document.createElement("div");
  group.className = "vx-studio-checkbox-group";
  group.id = id;
  for (const item of items) {
    const label = document.createElement("label");
    label.className = "vx-studio-checkbox";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.value = item.value;
    box.checked = item.checked;
    const text = document.createElement("span");
    text.textContent = item.label;
    if (item.hint) text.title = item.hint;
    label.append(box, text);
    group.append(label);
  }
  return group;
}

function dynamicTextRows(id: string, label: string, placeholder: string): HTMLElement {
  const group = document.createElement("div");
  group.className = "vx-studio-dynamic-rows";
  group.id = id;
  const heading = document.createElement("span");
  heading.className = "vx-studio-dynamic-rows-label";
  heading.textContent = label;
  const rows = document.createElement("div");
  rows.className = "vx-studio-dynamic-rows-list";
  const add = document.createElement("button");
  add.type = "button";
  add.className = "vx-studio-row-add";
  add.textContent = `＋ 添加${label}`;
  add.addEventListener("click", () => rows.append(buildRow()));
  function buildRow(): HTMLElement {
    const row = document.createElement("div");
    row.className = "vx-studio-dynamic-row";
    const input = document.createElement("input");
    input.type = "text";
    input.placeholder = placeholder;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "vx-studio-row-remove";
    remove.textContent = "－";
    remove.addEventListener("click", () => row.remove());
    row.append(input, remove);
    return row;
  }
  group.append(heading, rows, add);
  return group;
}

function renderDiagnostics(messages: string[]): void {
  const target = studioRoot()?.querySelector<HTMLElement>("#studio-diagnostics");
  if (target) target.textContent = messages.join("\n");
}

function formatDiagnostic(item: AgentProfileDiagnostic): string {
  return item.path ? `${item.path}: ${item.message}` : item.message;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function checkedValues(root: HTMLElement, selector: string): string[] {
  return [...root.querySelectorAll<HTMLInputElement>(`${selector} input[type="checkbox"]:checked`)]
    .map((box) => box.value);
}

function dynamicRowValues(group: HTMLElement | null): string[] {
  if (!group) return [];
  return [...group.querySelectorAll<HTMLInputElement>("input[type='text']")]
    .map((input) => input.value);
}

function collectFormState(root: HTMLElement, catalog: AgentCatalog): StudioFormState {
  const q = <T extends HTMLElement>(selector: string): T | null => root.querySelector<T>(selector);
  const workflowMode = (q<HTMLInputElement>('input[name="studio-workflow-mode"]:checked')?.value
    ?? "default") as StudioFormState["workflowMode"];
  const linearSteps: LinearStepState[] = [...root.querySelectorAll<HTMLElement>(".vx-studio-step")]
    .map((step) => ({
      kind: (step.querySelector<HTMLSelectElement>(".vx-studio-step-kind")?.value === "custom"
        ? "custom"
        : "ref") as LinearStepState["kind"],
      ref: step.querySelector<HTMLSelectElement>(".vx-studio-step-ref")?.value ?? "",
      condition: step.querySelector<HTMLInputElement>(".vx-studio-step-condition")?.value ?? "completed",
      custom: {
        name: step.querySelector<HTMLInputElement>(".vx-studio-step-custom-name")?.value ?? "",
        goal: step.querySelector<HTMLInputElement>(".vx-studio-step-custom-goal")?.value ?? "",
        description: step.querySelector<HTMLInputElement>(".vx-studio-step-custom-description")?.value ?? "",
        persona: step.querySelector<HTMLInputElement>(".vx-studio-step-custom-persona")?.value ?? "",
        steps: (step.querySelector<HTMLTextAreaElement>(".vx-studio-step-custom-steps")?.value ?? "").split("\n"),
        rules: (step.querySelector<HTMLTextAreaElement>(".vx-studio-step-custom-rules")?.value ?? "").split("\n"),
      },
    }));
  const checkedTools = new Set(checkedValues(root, "#studio-tools"));
  return {
    name: q<HTMLInputElement>("#studio-name")?.value ?? "",
    displayName: q<HTMLInputElement>("#studio-display-name")?.value ?? "",
    promptPolicy: q<HTMLSelectElement>("#studio-prompt-policy")?.value ?? "coding",
    identity: q<HTMLTextAreaElement>("#studio-identity")?.value ?? "",
    persona: q<HTMLInputElement>("#studio-persona")?.value ?? "",
    styleRules: dynamicRowValues(q("#studio-style-rules")),
    extraRules: dynamicRowValues(q("#studio-extra-rules")),
    suppressSections: dynamicRowValues(q("#studio-suppress-sections")),
    runMode: q<HTMLInputElement>('input[name="studio-run-mode"]:checked')?.value ?? "single",
    hitlMode: q<HTMLInputElement>('input[name="studio-hitl"]:checked')?.value ?? "interactive",
    workflowMode,
    builtinOrder: catalog.builtin_nodes.map((node) => node.name),
    builtinSelected: checkedValues(root, "#studio-workflow-builtin"),
    defaultEdges: catalog.default_edges,
    linearSteps,
    uncheckedToolIds: catalog.tools.map((tool) => tool.id).filter((id) => !checkedTools.has(id)),
    selectedSkills: checkedValues(root, "#studio-skills"),
    selectedMcpServers: checkedValues(root, "#studio-mcp"),
  };
}

function collectSaveInput(): {
  scope: string;
  name: string;
  payload: Record<string, unknown>;
  expected_revision: number;
} | null {
  const root = studioRoot();
  if (!root || !studioState) return null;
  const form = collectFormState(root, studioState.catalog);
  const name = form.name.trim().toLowerCase();
  if (!/^[a-z0-9][a-z0-9-]*$/.test(name)) {
    renderDiagnostics(["标识名称必填，且只能包含小写字母、数字和连字符"]);
    return null;
  }
  const scope = root.querySelector<HTMLInputElement>('input[name="studio-scope"]:checked')?.value ?? "project";
  // 新建约定：expected_revision 0 表示仅当目标不存在时才创建
  return { scope, name, payload: buildAgentProfilePayload(form), expected_revision: 0 };
}

async function runStudioValidate(): Promise<boolean> {
  const input = collectSaveInput();
  if (!input || !studioState) return false;
  try {
    const result = await studioState.rpc.validate(input);
    renderDiagnostics(result.diagnostics.map(formatDiagnostic));
    return result.valid;
  } catch (error) {
    renderDiagnostics([errorMessage(error)]);
    return false;
  }
}

async function runStudioSave(): Promise<void> {
  const input = collectSaveInput();
  if (!input || !studioState) return;
  try {
    const validation = await studioState.rpc.validate(input);
    if (!validation.valid) {
      renderDiagnostics(validation.diagnostics.map(formatDiagnostic));
      return;
    }
    const saved = await studioState.rpc.save(input);
    const warnings = (saved.diagnostics ?? []).map(formatDiagnostic);
    if (warnings.length) renderDiagnostics(warnings);
    const { onSaved } = studioState;
    closeAgentStudio();
    onSaved(input.name);
  } catch (error) {
    const message = errorMessage(error);
    const hint = message.includes("conflict") ? "同名 Agent 可能已存在，请更换标识名称" : "";
    renderDiagnostics([message, hint].filter(Boolean));
  }
}

export function _resetAgentStudioForTest(): void {
  studioState = null;
}
