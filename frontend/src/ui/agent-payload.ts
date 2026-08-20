/**
 * Agent Studio 表单状态 → agent profile payload 的纯函数组装。
 * 规则：空可选字段省略；工具未勾选项进 block；技能/MCP 不勾=不限制；
 * 内置节点子集按 DAG 序连成链（有直连默认边则保留，否则补 completed）；
 * 线性模式逐步串联并以 terminal_exit 收尾。
 */

export interface AgentCatalogEdgeInput {
  source: string;
  target: string;
  condition: string;
  label: string;
}

export interface LinearCustomNodeState {
  name: string;
  goal: string;
  description: string;
  persona: string;
  steps: string[];
  rules: string[];
}

export interface LinearStepState {
  kind: "ref" | "custom";
  ref: string;
  condition: string;
  custom: LinearCustomNodeState;
}

export interface StudioFormState {
  name: string;
  displayName: string;
  promptPolicy: string;
  identity: string;
  persona: string;
  styleRules: string[];
  extraRules: string[];
  suppressSections: string[];
  runMode: string;
  hitlMode: string;
  workflowMode: "default" | "builtin" | "linear";
  builtinOrder: string[];
  builtinSelected: string[];
  defaultEdges: AgentCatalogEdgeInput[];
  linearSteps: LinearStepState[];
  uncheckedToolIds: string[];
  selectedSkills: string[];
  selectedMcpServers: string[];
}

function cleanLines(lines: string[]): string[] {
  return lines.map((line) => line.trim()).filter((line) => line.length > 0);
}

function normalizeNodeName(name: string): string {
  return name.trim().toLowerCase();
}

export function buildAgentProfilePayload(state: StudioFormState): Record<string, unknown> {
  const payload: Record<string, unknown> = {
    name: normalizeNodeName(state.name),
    revision: 1,
    display_name: state.displayName.trim(),
    prompt_policy: state.promptPolicy,
    run_mode: state.runMode,
    hitl_mode: state.hitlMode,
  };
  const identity = state.identity.trim();
  if (identity) payload.identity = identity;
  const persona = state.persona.trim();
  if (persona) payload.persona = persona;
  const styleRules = cleanLines(state.styleRules);
  if (styleRules.length) payload.style_rules = styleRules;
  const extraRules = cleanLines(state.extraRules);
  if (extraRules.length) payload.extra_rules = extraRules;
  const suppressSections = cleanLines(state.suppressSections);
  if (suppressSections.length) payload.suppress_sections = suppressSections;
  if (state.uncheckedToolIds.length) payload.tools = { block: [...state.uncheckedToolIds] };
  if (state.selectedSkills.length) payload.skills = [...state.selectedSkills];
  if (state.selectedMcpServers.length) payload.mcp_servers = [...state.selectedMcpServers];
  const workflow = buildWorkflow(state);
  if (workflow) payload.workflow = workflow;
  return payload;
}

function buildWorkflow(state: StudioFormState): Record<string, unknown> | null {
  if (state.workflowMode === "builtin") return buildBuiltinWorkflow(state);
  if (state.workflowMode === "linear") return buildLinearWorkflow(state);
  return null;
}

function buildBuiltinWorkflow(state: StudioFormState): Record<string, unknown> | null {
  const selected = state.builtinOrder.filter((name) => state.builtinSelected.includes(name));
  if (selected.length === 0) return null;
  const nodes = selected.map((name) => ({ ref: name }));
  const inherited: AgentCatalogEdgeInput[] = [];
  const gapFilled: AgentCatalogEdgeInput[] = [];
  for (let index = 0; index < selected.length - 1; index += 1) {
    const source = selected[index];
    const target = selected[index + 1];
    const direct = state.defaultEdges.find(
      (edge) => edge.source === source && edge.target === target,
    );
    if (direct) {
      inherited.push(direct);
    } else {
      gapFilled.push({ source, target, condition: "completed", label: "completed" });
    }
  }
  return { nodes, edges: [...inherited, ...gapFilled] };
}

function buildLinearWorkflow(state: StudioFormState): Record<string, unknown> | null {
  if (state.linearSteps.length === 0) return null;
  const nodes: Array<Record<string, unknown>> = [];
  const names: string[] = [];
  for (const step of state.linearSteps) {
    if (step.kind === "ref") {
      const ref = normalizeNodeName(step.ref);
      nodes.push({ ref });
      names.push(ref);
      continue;
    }
    const custom = step.custom;
    const name = normalizeNodeName(custom.name);
    const node: Record<string, unknown> = {
      name,
      goal: custom.goal.trim(),
      description: custom.description.trim(),
      persona: custom.persona.trim(),
    };
    const steps = cleanLines(custom.steps).map((action, index) => ({ order: index + 1, action }));
    if (steps.length) node.workflow = steps;
    const rules = cleanLines(custom.rules);
    if (rules.length) node.rules = rules;
    nodes.push(node);
    names.push(name);
  }
  const edges = state.linearSteps.slice(0, -1).map((step, index) => {
    const condition = step.condition.trim() || "completed";
    return { source: names[index], target: names[index + 1], condition, label: condition };
  });
  return {
    name: "custom",
    nodes,
    edges,
    terminal_exit: { condition: "done", label: "end" },
  };
}
