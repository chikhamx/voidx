import { describe, expect, it } from "vitest";
import {
  buildAgentProfilePayload,
  type StudioFormState,
} from "../../src/ui/agent-payload";

const EDGES = [
  { source: "brainstorm", target: "design", condition: "approved", label: "design approved" },
  { source: "brainstorm", target: "tdd", condition: "small_change", label: "small change" },
  { source: "design", target: "plan", condition: "completed", label: "doc done" },
  { source: "plan", target: "tdd", condition: "approved", label: "plan approved" },
  { source: "tdd", target: "verify", condition: "green", label: "tests green" },
];

const BUILTIN_ORDER = ["brainstorm", "design", "plan", "tdd", "verify", "review", "feedback", "debug"];

function baseState(overrides: Partial<StudioFormState> = {}): StudioFormState {
  return {
    name: "my-agent",
    displayName: "My Agent",
    promptPolicy: "coding",
    identity: "",
    persona: "",
    styleRules: [],
    extraRules: [],
    suppressSections: [],
    runMode: "single",
    hitlMode: "interactive",
    workflowMode: "default",
    builtinOrder: BUILTIN_ORDER,
    builtinSelected: [],
    defaultEdges: EDGES,
    linearSteps: [],
    uncheckedToolIds: [],
    selectedSkills: [],
    selectedMcpServers: [],
    ...overrides,
  };
}

describe("buildAgentProfilePayload", () => {
  it("assembles the minimal payload with only required fields", () => {
    const payload = buildAgentProfilePayload(baseState());
    expect(payload).toEqual({
      name: "my-agent",
      revision: 1,
      display_name: "My Agent",
      prompt_policy: "coding",
      run_mode: "single",
      hitl_mode: "interactive",
    });
  });

  it("includes prompt fields only when non-empty and filters blank rule lines", () => {
    const payload = buildAgentProfilePayload(baseState({
      identity: "  代码审查专家  ",
      persona: "严谨",
      styleRules: ["简洁", "", "  ", "直接"],
      extraRules: ["不改无关文件"],
    }));
    expect(payload.identity).toBe("代码审查专家");
    expect(payload.persona).toBe("严谨");
    expect(payload.style_rules).toEqual(["简洁", "直接"]);
    expect(payload.extra_rules).toEqual(["不改无关文件"]);
    expect("suppress_sections" in payload).toBe(false);
  });

  it("omits tools when everything is checked and emits block list for unchecked", () => {
    expect("tools" in buildAgentProfilePayload(baseState())).toBe(false);
    const payload = buildAgentProfilePayload(baseState({ uncheckedToolIds: ["bash", "agent"] }));
    expect(payload.tools).toEqual({ block: ["bash", "agent"] });
  });

  it("omits skills/mcp when nothing is selected and includes checked subsets", () => {
    const empty = buildAgentProfilePayload(baseState());
    expect("skills" in empty).toBe(false);
    expect("mcp_servers" in empty).toBe(false);
    const payload = buildAgentProfilePayload(baseState({
      selectedSkills: ["react-patterns"],
      selectedMcpServers: ["web-search"],
    }));
    expect(payload.skills).toEqual(["react-patterns"]);
    expect(payload.mcp_servers).toEqual(["web-search"]);
  });

  it("builtin mode inherits default edges for the selected subset in DAG order", () => {
    const payload = buildAgentProfilePayload(baseState({
      workflowMode: "builtin",
      builtinSelected: ["tdd", "brainstorm", "plan"],
    }));
    const workflow = payload.workflow as {
      nodes: Array<{ ref: string }>;
      edges: Array<{ source: string; target: string; condition: string }>;
    };
    expect(workflow.nodes).toEqual([{ ref: "brainstorm" }, { ref: "plan" }, { ref: "tdd" }]);
    // 直接默认边 plan→tdd 保留；brainstorm→plan 无直接默认边，补 completed
    expect(workflow.edges).toEqual([
      { source: "plan", target: "tdd", condition: "approved", label: "plan approved" },
      { source: "brainstorm", target: "plan", condition: "completed", label: "completed" },
    ]);
  });

  it("builtin mode keeps direct default edges without gap-filling", () => {
    const payload = buildAgentProfilePayload(baseState({
      workflowMode: "builtin",
      builtinSelected: ["brainstorm", "design"],
    }));
    const workflow = payload.workflow as { edges: Array<{ condition: string }> };
    expect(workflow.edges).toEqual([
      { source: "brainstorm", target: "design", condition: "approved", label: "design approved" },
    ]);
  });

  it("builtin mode with zero or one selection omits the workflow field", () => {
    expect("workflow" in buildAgentProfilePayload(baseState({ workflowMode: "builtin" }))).toBe(false);
    const single = buildAgentProfilePayload(baseState({
      workflowMode: "builtin",
      builtinSelected: ["tdd"],
    }));
    const workflow = single.workflow as { nodes: unknown[]; edges: unknown[] };
    expect(workflow.nodes).toEqual([{ ref: "tdd" }]);
    expect(workflow.edges).toEqual([]);
  });

  it("linear mode chains steps into sequential edges with a terminal exit", () => {
    const payload = buildAgentProfilePayload(baseState({
      workflowMode: "linear",
      linearSteps: [
        {
          kind: "ref",
          ref: "brainstorm",
          condition: "approved",
          custom: { name: "", goal: "", description: "", persona: "", steps: [], rules: [] },
        },
        {
          kind: "custom",
          ref: "",
          condition: "completed",
          custom: {
            name: "Polish",
            goal: "打磨实现细节",
            description: "润色代码与文档",
            persona: "细致",
            steps: ["检查命名", "  ", "补齐注释"],
            rules: ["不改公共 API"],
          },
        },
      ],
    }));
    const workflow = payload.workflow as {
      name: string;
      nodes: unknown[];
      edges: Array<{ source: string; target: string; condition: string; label: string }>;
      terminal_exit: { condition: string; label: string };
    };
    expect(workflow.name).toBe("custom");
    expect(workflow.nodes).toEqual([
      { ref: "brainstorm" },
      {
        name: "polish",
        goal: "打磨实现细节",
        description: "润色代码与文档",
        persona: "细致",
        workflow: [
          { order: 1, action: "检查命名" },
          { order: 2, action: "补齐注释" },
        ],
        rules: ["不改公共 API"],
      },
    ]);
    expect(workflow.edges).toEqual([
      { source: "brainstorm", target: "polish", condition: "approved", label: "approved" },
    ]);
    expect(workflow.terminal_exit).toEqual({ condition: "done", label: "end" });
  });

  it("linear mode with no steps omits the workflow field", () => {
    expect("workflow" in buildAgentProfilePayload(baseState({ workflowMode: "linear" }))).toBe(false);
  });
});
