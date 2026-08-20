import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  closeAgentStudio,
  openAgentStudio,
  type AgentCatalog,
  type AgentStudioRpc,
} from "../../src/ui/agent-studio";

const catalog: AgentCatalog = {
  tools: [
    { id: "read", description: "Read files" },
    { id: "bash", description: "Run shell commands" },
    { id: "todo", description: "Track tasks" },
  ],
  skills: [{ name: "react-patterns", description: "React conventions" }],
  mcp_servers: [{ name: "web-search", description: "search tools" }],
  builtin_nodes: [
    { name: "brainstorm", description: "Explore requirements" },
    { name: "plan", description: "Plan implementation" },
    { name: "tdd", description: "Implement via TDD" },
  ],
  default_edges: [
    { source: "brainstorm", target: "plan", condition: "approved", label: "approved" },
    { source: "plan", target: "tdd", condition: "approved", label: "plan approved" },
  ],
};

type MockedStudioRpc = AgentStudioRpc & {
  [K in keyof AgentStudioRpc]: AgentStudioRpc[K] & ReturnType<typeof vi.fn>;
};

function studioRpc(overrides: Partial<AgentStudioRpc> = {}): MockedStudioRpc {
  return {
    getCatalog: vi.fn().mockResolvedValue(catalog),
    validate: vi.fn().mockResolvedValue({ valid: true, diagnostics: [] }),
    save: vi.fn().mockResolvedValue({ diagnostics: [] }),
    ...overrides,
  } as MockedStudioRpc;
}

async function flush(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

describe("agent studio page", () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <section id="empty-state"></section>
      <div id="transcript"></div>
      <form id="composer"></form>
      <section id="agent-studio" hidden></section>`;
  });

  it("opens as a full page: hides chat elements and renders header with a back button", async () => {
    await openAgentStudio({ rpc: studioRpc(), onSaved: vi.fn() });
    await flush();

    expect(document.querySelector<HTMLElement>("#empty-state")!.hidden).toBe(true);
    expect(document.querySelector<HTMLElement>("#transcript")!.hidden).toBe(true);
    expect(document.querySelector<HTMLElement>("#composer")!.hidden).toBe(true);
    const studio = document.querySelector<HTMLElement>("#agent-studio")!;
    expect(studio.hidden).toBe(false);
    expect(studio.querySelector(".vx-agent-studio-back")?.textContent).toContain("返回");
    expect(studio.querySelector(".vx-agent-studio-title")?.textContent).toContain("新建自定义 Agent");
  });

  it("back button restores chat elements", async () => {
    await openAgentStudio({ rpc: studioRpc(), onSaved: vi.fn() });
    await flush();

    document.querySelector<HTMLButtonElement>(".vx-agent-studio-back")!.click();

    expect(document.querySelector<HTMLElement>("#agent-studio")!.hidden).toBe(true);
    expect(document.querySelector<HTMLElement>("#transcript")!.hidden).toBe(false);
    expect(document.querySelector<HTMLElement>("#composer")!.hidden).toBe(false);
  });

  it("closeAgentStudio is idempotent when the studio was never opened", () => {
    expect(() => closeAgentStudio()).not.toThrow();
  });

  it("renders all form sections after loading the catalog", async () => {
    await openAgentStudio({ rpc: studioRpc(), onSaved: vi.fn() });
    await flush();

    const studio = document.querySelector<HTMLElement>("#agent-studio")!;
    const sectionTitles = [...studio.querySelectorAll("h3")].map((el) => el.textContent);
    expect(sectionTitles).toEqual(
      expect.arrayContaining(["基本信息", "Prompt 风格", "运行方式", "工作流", "工具权限"]),
    );
    // Basic info fields
    expect(studio.querySelector("#studio-name")).not.toBeNull();
    expect(studio.querySelector("#studio-display-name")).not.toBeNull();
    expect(studio.querySelectorAll('input[name="studio-scope"]')).toHaveLength(2);
    // Prompt fields
    expect(studio.querySelector("#studio-prompt-policy")).not.toBeNull();
    expect(studio.querySelector("#studio-identity")).not.toBeNull();
    // Run mode radios
    expect(studio.querySelectorAll('input[name="studio-run-mode"]')).toHaveLength(3);
    expect(studio.querySelectorAll('input[name="studio-hitl"]')).toHaveLength(2);
    // Tool checkboxes default to checked; skills/mcp default to unchecked
    const toolBoxes = [...studio.querySelectorAll<HTMLInputElement>('#studio-tools input[type="checkbox"]')];
    expect(toolBoxes).toHaveLength(3);
    expect(toolBoxes.every((box) => box.checked)).toBe(true);
    const skillBoxes = [...studio.querySelectorAll<HTMLInputElement>('#studio-skills input[type="checkbox"]')];
    expect(skillBoxes).toHaveLength(1);
    expect(skillBoxes.every((box) => !box.checked)).toBe(true);
  });
});


describe("agent studio prompt sections", () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <section id="empty-state"></section>
      <div id="transcript"></div>
      <form id="composer"></form>
      <section id="agent-studio" hidden></section>`;
  });

  it("renders dynamic rule rows with add/remove for style_rules, extra_rules, suppress_sections", async () => {
    await openAgentStudio({ rpc: studioRpc(), onSaved: vi.fn() });
    await flush();

    for (const listId of ["studio-style-rules", "studio-extra-rules", "studio-suppress-sections"]) {
      const group = document.querySelector<HTMLElement>(`#${listId}`);
      expect(group, listId).not.toBeNull();
      const addButton = group!.querySelector<HTMLButtonElement>(".vx-studio-row-add");
      expect(addButton, `${listId} add button`).not.toBeNull();

      // Add two rows, fill them, remove the first
      addButton!.click();
      addButton!.click();
      let inputs = [...group!.querySelectorAll<HTMLInputElement>("input[type='text']")];
      expect(inputs).toHaveLength(2);
      inputs[0].value = "规则一";
      inputs[1].value = "规则二";
      group!.querySelector<HTMLButtonElement>(".vx-studio-row-remove")!.click();
      inputs = [...group!.querySelectorAll<HTMLInputElement>("input[type='text']")];
      expect(inputs).toHaveLength(1);
      expect(inputs[0].value).toBe("规则二");
    }
  });
});


describe("agent studio workflow section", () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <section id="empty-state"></section>
      <div id="transcript"></div>
      <form id="composer"></form>
      <section id="agent-studio" hidden></section>`;
  });

  async function openStudio(): Promise<HTMLElement> {
    await openAgentStudio({ rpc: studioRpc(), onSaved: vi.fn() });
    await flush();
    return document.querySelector<HTMLElement>("#agent-studio")!;
  }

  function selectWorkflowMode(studio: HTMLElement, mode: string): void {
    const radio = studio.querySelector<HTMLInputElement>(`input[name="studio-workflow-mode"][value="${mode}"]`)!;
    radio.checked = true;
    radio.dispatchEvent(new Event("change"));
  }

  it("switching workflow mode toggles the builtin and linear panels", async () => {
    const studio = await openStudio();
    const builtinPanel = studio.querySelector<HTMLElement>("#studio-workflow-builtin")!;
    const linearPanel = studio.querySelector<HTMLElement>("#studio-workflow-linear")!;
    expect(builtinPanel.hidden).toBe(true);
    expect(linearPanel.hidden).toBe(true);

    selectWorkflowMode(studio, "builtin");
    expect(builtinPanel.hidden).toBe(false);
    expect(linearPanel.hidden).toBe(true);

    selectWorkflowMode(studio, "linear");
    expect(builtinPanel.hidden).toBe(true);
    expect(linearPanel.hidden).toBe(false);

    selectWorkflowMode(studio, "default");
    expect(builtinPanel.hidden).toBe(true);
    expect(linearPanel.hidden).toBe(true);
  });

  it("builtin mode renders a checkbox card per builtin node, unchecked by default", async () => {
    const studio = await openStudio();
    selectWorkflowMode(studio, "builtin");
    const boxes = [...studio.querySelectorAll<HTMLInputElement>('#studio-workflow-builtin input[type="checkbox"]')];
    expect(boxes).toHaveLength(3);
    expect(boxes.map((box) => box.value)).toEqual(["brainstorm", "plan", "tdd"]);
    expect(boxes.every((box) => !box.checked)).toBe(true);
    expect(studio.querySelector("#studio-workflow-builtin")!.textContent).toContain("Explore requirements");
  });

  it("linear mode supports add/remove/reorder steps with ref or custom node forms", async () => {
    const studio = await openStudio();
    selectWorkflowMode(studio, "linear");

    const addButton = studio.querySelector<HTMLButtonElement>(".vx-studio-step-add")!;
    expect(addButton).not.toBeNull();
    addButton.click();
    addButton.click();

    let steps = [...studio.querySelectorAll<HTMLElement>(".vx-studio-step")];
    expect(steps).toHaveLength(2);
    // Each step has: 序号、类型选择、内置节点下拉、完成条件、上移/下移/删除
    const first = steps[0];
    expect(first.querySelector(".vx-studio-step-order")?.textContent).toBe("1");
    expect(first.querySelectorAll('select.vx-studio-step-kind option')).toHaveLength(2);
    const refSelect = first.querySelector<HTMLSelectElement>("select.vx-studio-step-ref")!;
    expect([...refSelect.options].map((option) => option.value)).toEqual(["brainstorm", "plan", "tdd"]);
    expect(first.querySelector<HTMLInputElement>("input.vx-studio-step-condition")!.value).toBe("completed");
    expect(first.querySelector(".vx-studio-step-up")).not.toBeNull();
    expect(first.querySelector(".vx-studio-step-down")).not.toBeNull();
    expect(first.querySelector(".vx-studio-step-remove")).not.toBeNull();

    // 选择 ref 后序列化可见；切换为自定义节点后展示自定义表单
    refSelect.value = "tdd";
    refSelect.dispatchEvent(new Event("change"));
    const kindSelect = first.querySelector<HTMLSelectElement>("select.vx-studio-step-kind")!;
    kindSelect.value = "custom";
    kindSelect.dispatchEvent(new Event("change"));
    expect(first.querySelector<HTMLElement>(".vx-studio-step-custom")!.hidden).toBe(false);
    expect(first.querySelector<HTMLInputElement>("input.vx-studio-step-custom-name")).not.toBeNull();
    expect(first.querySelector<HTMLInputElement>("input.vx-studio-step-custom-goal")).not.toBeNull();

    // 上移第二步到首位，序号重排
    const secondUp = steps[1].querySelector<HTMLButtonElement>(".vx-studio-step-up")!;
    const secondKind = steps[1].querySelector<HTMLSelectElement>("select.vx-studio-step-kind")!;
    secondKind.value = "custom";
    secondKind.dispatchEvent(new Event("change"));
    steps[1].querySelector<HTMLInputElement>("input.vx-studio-step-custom-name")!.value = "polish";
    secondUp.click();
    steps = [...studio.querySelectorAll<HTMLElement>(".vx-studio-step")];
    expect(steps[0].querySelector<HTMLInputElement>("input.vx-studio-step-custom-name")!.value).toBe("polish");
    expect(steps[0].querySelector(".vx-studio-step-order")?.textContent).toBe("1");
    expect(steps[1].querySelector(".vx-studio-step-order")?.textContent).toBe("2");

    // 删除首步后剩余一步
    steps[0].querySelector<HTMLButtonElement>(".vx-studio-step-remove")!.click();
    steps = [...studio.querySelectorAll<HTMLElement>(".vx-studio-step")];
    expect(steps).toHaveLength(1);
    expect(steps[0].querySelector(".vx-studio-step-order")?.textContent).toBe("1");
  });
});


describe("agent studio save flow", () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <section id="empty-state"></section>
      <div id="transcript"></div>
      <form id="composer"></form>
      <section id="agent-studio" hidden></section>`;
  });

  function fillBasics(studio: HTMLElement): void {
    studio.querySelector<HTMLInputElement>("#studio-name")!.value = "my-agent";
    studio.querySelector<HTMLInputElement>("#studio-display-name")!.value = "My Agent";
  }

  it("save validates first, persists via payload, then closes and fires onSaved", async () => {
    const rpc = studioRpc();
    const onSaved = vi.fn();
    await openAgentStudio({ rpc, onSaved });
    await flush();
    const studio = document.querySelector<HTMLElement>("#agent-studio")!;
    fillBasics(studio);
    studio.querySelector<HTMLInputElement>('#studio-tools input[value="bash"]')!.checked = false;
    studio.querySelector<HTMLInputElement>('#studio-skills input[value="react-patterns"]')!.checked = true;

    studio.querySelector<HTMLButtonElement>("#studio-save")!.click();
    await flush();

    expect(rpc.validate).toHaveBeenCalledTimes(1);
    expect(rpc.validate.mock.calls[0][0].scope).toBe("project");
    expect(rpc.save).toHaveBeenCalledTimes(1);
    const saveParams = rpc.save.mock.calls[0][0];
    expect(saveParams).toMatchObject({ scope: "project", name: "my-agent", expected_revision: 0 });
    expect(saveParams.payload).toMatchObject({
      name: "my-agent",
      revision: 1,
      display_name: "My Agent",
      prompt_policy: "coding",
      run_mode: "single",
      hitl_mode: "interactive",
      tools: { block: ["bash"] },
      skills: ["react-patterns"],
    });
    expect(onSaved).toHaveBeenCalledWith("my-agent");
    expect(document.querySelector<HTMLElement>("#agent-studio")!.hidden).toBe(true);
    expect(document.querySelector<HTMLElement>("#composer")!.hidden).toBe(false);
  });

  it("validate button renders diagnostics without saving", async () => {
    const rpc = studioRpc({
      validate: vi.fn().mockResolvedValue({
        valid: false,
        diagnostics: [{ path: "tools.block", code: "unknown_tool", message: "未知工具: bash" }],
      }),
    });
    await openAgentStudio({ rpc, onSaved: vi.fn() });
    await flush();
    const studio = document.querySelector<HTMLElement>("#agent-studio")!;
    fillBasics(studio);

    studio.querySelector<HTMLButtonElement>("#studio-validate")!.click();
    await flush();

    expect(rpc.validate).toHaveBeenCalledTimes(1);
    expect(rpc.save).not.toHaveBeenCalled();
    expect(studio.querySelector("#studio-diagnostics")!.textContent).toContain("未知工具: bash");
    expect(studio.hidden).toBe(false);
  });

  it("failed validation on save does not persist", async () => {
    const rpc = studioRpc({
      validate: vi.fn().mockResolvedValue({
        valid: false,
        diagnostics: [{ path: "workflow", code: "unknown_ref", message: "引用了不存在的节点" }],
      }),
    });
    const onSaved = vi.fn();
    await openAgentStudio({ rpc, onSaved });
    await flush();
    const studio = document.querySelector<HTMLElement>("#agent-studio")!;
    fillBasics(studio);

    studio.querySelector<HTMLButtonElement>("#studio-save")!.click();
    await flush();

    expect(rpc.save).not.toHaveBeenCalled();
    expect(onSaved).not.toHaveBeenCalled();
    expect(studio.querySelector("#studio-diagnostics")!.textContent).toContain("引用了不存在的节点");
  });

  it("blocks save when the name is missing or invalid without any RPC call", async () => {
    const rpc = studioRpc();
    await openAgentStudio({ rpc, onSaved: vi.fn() });
    await flush();
    const studio = document.querySelector<HTMLElement>("#agent-studio")!;

    studio.querySelector<HTMLButtonElement>("#studio-save")!.click();
    await flush();
    expect(rpc.validate).not.toHaveBeenCalled();
    expect(studio.querySelector("#studio-diagnostics")!.textContent).toContain("标识名称");

    studio.querySelector<HTMLInputElement>("#studio-name")!.value = "My Agent!!";
    studio.querySelector<HTMLButtonElement>("#studio-save")!.click();
    await flush();
    expect(rpc.validate).not.toHaveBeenCalled();
  });

  it("linear workflow steps serialize into the payload", async () => {
    const rpc = studioRpc();
    await openAgentStudio({ rpc, onSaved: vi.fn() });
    await flush();
    const studio = document.querySelector<HTMLElement>("#agent-studio")!;
    fillBasics(studio);

    const radio = studio.querySelector<HTMLInputElement>('input[name="studio-workflow-mode"][value="linear"]')!;
    radio.checked = true;
    radio.dispatchEvent(new Event("change"));
    studio.querySelector<HTMLButtonElement>(".vx-studio-step-add")!.click();
    const step = studio.querySelector<HTMLElement>(".vx-studio-step")!;
    step.querySelector<HTMLSelectElement>("select.vx-studio-step-ref")!.value = "tdd";
    step.querySelector<HTMLInputElement>("input.vx-studio-step-condition")!.value = "approved";

    studio.querySelector<HTMLButtonElement>("#studio-save")!.click();
    await flush();

    const saveParams = rpc.save.mock.calls[0][0];
    expect(saveParams.payload.workflow).toEqual({
      name: "custom",
      nodes: [{ ref: "tdd" }],
      edges: [],
      terminal_exit: { condition: "done", label: "end" },
    });
  });
});


describe("agent studio robustness", () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <section id="empty-state"></section>
      <div id="transcript"></div>
      <form id="composer"></form>
      <section id="agent-studio" hidden></section>`;
  });

  it("opening the studio twice is a no-op and close still restores chat", async () => {
    const rpc = studioRpc();
    await openAgentStudio({ rpc, onSaved: vi.fn() });
    await flush();
    await openAgentStudio({ rpc, onSaved: vi.fn() });
    await flush();

    expect(rpc.getCatalog).toHaveBeenCalledTimes(1);
    closeAgentStudio();
    expect(document.querySelector<HTMLElement>("#transcript")!.hidden).toBe(false);
    expect(document.querySelector<HTMLElement>("#composer")!.hidden).toBe(false);
  });

  it("catalog load failure shows an error page instead of an unhandled rejection", async () => {
    const rpc = studioRpc({ getCatalog: vi.fn().mockRejectedValue(new Error("gateway offline")) });
    await openAgentStudio({ rpc, onSaved: vi.fn() });
    await flush();

    const studio = document.querySelector<HTMLElement>("#agent-studio")!;
    expect(studio.hidden).toBe(false);
    expect(studio.textContent).toContain("gateway offline");
    // 返回按钮可用，聊天区可恢复
    studio.querySelector<HTMLButtonElement>(".vx-agent-studio-back")!.click();
    expect(document.querySelector<HTMLElement>("#transcript")!.hidden).toBe(false);
  });
});


describe("agent studio conflict handling", () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <section id="empty-state"></section>
      <div id="transcript"></div>
      <form id="composer"></form>
      <section id="agent-studio" hidden></section>`;
  });

  it("save conflict surfaces a name-collision hint and keeps the studio open", async () => {
    const rpc = studioRpc({ save: vi.fn().mockRejectedValue(new Error("agent profile conflict")) });
    const onSaved = vi.fn();
    await openAgentStudio({ rpc, onSaved });
    await flush();
    const studio = document.querySelector<HTMLElement>("#agent-studio")!;
    studio.querySelector<HTMLInputElement>("#studio-name")!.value = "my-agent";
    studio.querySelector<HTMLInputElement>("#studio-display-name")!.value = "My Agent";

    studio.querySelector<HTMLButtonElement>("#studio-save")!.click();
    await flush();

    expect(onSaved).not.toHaveBeenCalled();
    expect(studio.hidden).toBe(false);
    const diagnostics = studio.querySelector("#studio-diagnostics")!.textContent!;
    expect(diagnostics).toContain("agent profile conflict");
    expect(diagnostics).toContain("同名");
  });
});
