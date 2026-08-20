// @ts-nocheck
import { beforeEach, describe, expect, it, vi } from "vitest";
import { _resetSettingsForTest, initSettingsModal, renderSettingsModal } from "../../src/ui/settings";

const projectProfile = {
  name: "reviewer-v2",
  display_name: "Reviewer V2",
  revision: 3,
  content_hash: "hash-3",
  source: "project",
  run_mode: "review",
  hitl_mode: "interactive",
  availability: "available",
  diagnostics: [],
};
const bundledProfile = {
  ...projectProfile,
  name: "bundled-default",
  display_name: "Bundled Default",
  source: "bundled",
  content_hash: "bundled-hash",
};

async function flush(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
}

function profilesTab(): HTMLButtonElement {
  let tab = document.querySelector<HTMLButtonElement>('.settings-tab[data-tab="agent-profiles"]');
  if (!tab) {
    tab = document.createElement("button");
    tab.type = "button";
    tab.className = "settings-tab";
    tab.dataset.tab = "agent-profiles";
    tab.textContent = "Agent Profiles";
    document.querySelector("#settings-tabs")!.append(tab);
  }
  return tab;
}

describe("Agent Profiles YAML settings", () => {
  beforeEach(() => {
    _resetSettingsForTest();
    profilesTab();
  });

  it("lists profiles, gets YAML, and never receives or submits filesystem paths", async () => {
    const rpc = vi.fn(async (method, params) => {
      if (method === "list-agent-profiles") return { profiles: [projectProfile, bundledProfile] };
      if (method === "get-agent-profile") return { profile: projectProfile, yaml: "name: reviewer-v2\nrevision: 3\n", read_only: false };
      throw new Error(`unexpected ${method}`);
    });
    initSettingsModal({ agentProfileRpc: rpc });
    renderSettingsModal({});

    profilesTab().click();
    await flush();
    document.querySelector<HTMLButtonElement>('[data-agent-profile="reviewer-v2"]')!.click();
    await flush();

    expect(rpc).toHaveBeenCalledWith("list-agent-profiles", {});
    expect(rpc).toHaveBeenCalledWith("get-agent-profile", { scope: "project", name: "reviewer-v2" });
    expect(document.querySelector<HTMLTextAreaElement>("#agent-profile-yaml")!.value).toContain("revision: 3");
    expect(document.querySelector("#settings-content")!.textContent).toContain("revision 3");
    expect(document.querySelector("#settings-content")!.textContent).toContain("hash-3");
    expect(JSON.stringify(rpc.mock.calls)).not.toContain("path");
  });

  it("uses revision 0 when saving into a target scope that does not exist", async () => {
    const sourceYaml = "name: reviewer-v2\nrevision: 3\nnotes: keep editing\n";
    const rpc = vi.fn(async (method, params) => {
      if (method === "list-agent-profiles") return { profiles: [projectProfile] };
      if (method === "get-agent-profile" && params.scope === "project") {
        return { profile: projectProfile, yaml: sourceYaml, read_only: false };
      }
      if (method === "get-agent-profile" && params.scope === "global") {
        throw new Error("agent profile not found");
      }
      if (method === "save-agent-profile") return { snapshot: { revision: 1, source: "global", content_hash: "hash-1" }, diagnostics: [] };
      throw new Error(`unexpected ${method}`);
    });
    initSettingsModal({ agentProfileRpc: rpc });
    renderSettingsModal({});
    profilesTab().click();
    await flush();
    document.querySelector<HTMLButtonElement>('[data-agent-profile="reviewer-v2"]')!.click();
    await flush();

    const scope = document.querySelector<HTMLSelectElement>("#agent-profile-scope")!;
    scope.value = "global";
    scope.dispatchEvent(new Event("change"));
    await flush();

    expect(rpc).toHaveBeenCalledWith("get-agent-profile", { scope: "global", name: "reviewer-v2" });
    expect(document.querySelector<HTMLTextAreaElement>("#agent-profile-yaml")!.value).toBe(sourceYaml);
    document.querySelector<HTMLButtonElement>("#agent-profile-save")!.click();
    await flush();
    expect(rpc).toHaveBeenCalledWith("save-agent-profile", {
      scope: "global", name: "reviewer-v2", yaml: sourceYaml, expected_revision: 0,
    });
  });

  it("gets an existing target scope and saves with its revision without replacing edited YAML", async () => {
    const editedYaml = "name: reviewer-v2\nnotes: edited project content\n";
    const globalProfile = { ...projectProfile, source: "global", revision: 7, content_hash: "global-hash-7" };
    const rpc = vi.fn(async (method, params) => {
      if (method === "list-agent-profiles") return { profiles: [projectProfile] };
      if (method === "get-agent-profile" && params.scope === "project") {
        return { profile: projectProfile, yaml: "name: reviewer-v2\n", read_only: false };
      }
      if (method === "get-agent-profile" && params.scope === "global") {
        return { profile: globalProfile, yaml: "name: reviewer-v2\nnotes: global content\n", read_only: false };
      }
      if (method === "save-agent-profile") return { snapshot: { revision: 8, source: "global", content_hash: "global-hash-8" }, diagnostics: [] };
      throw new Error(`unexpected ${method}`);
    });
    initSettingsModal({ agentProfileRpc: rpc });
    renderSettingsModal({});
    profilesTab().click();
    await flush();
    document.querySelector<HTMLButtonElement>('[data-agent-profile="reviewer-v2"]')!.click();
    await flush();
    document.querySelector<HTMLTextAreaElement>("#agent-profile-yaml")!.value = editedYaml;

    const scope = document.querySelector<HTMLSelectElement>("#agent-profile-scope")!;
    scope.value = "global";
    scope.dispatchEvent(new Event("change"));
    await flush();

    expect(document.querySelector<HTMLTextAreaElement>("#agent-profile-yaml")!.value).toBe(editedYaml);
    document.querySelector<HTMLButtonElement>("#agent-profile-save")!.click();
    await flush();
    expect(rpc).toHaveBeenCalledWith("save-agent-profile", {
      scope: "global", name: "reviewer-v2", yaml: editedYaml, expected_revision: 7,
    });
  });

  it("surfaces a backend conflict when the selected target changes after scope switch", async () => {
    const globalProfile = { ...projectProfile, source: "global", revision: 7, content_hash: "global-hash-7" };
    const conflict = Object.assign(new Error("agent profile conflict"), {
      data: { current: { ...globalProfile, revision: 8, content_hash: "global-hash-8" } },
    });
    const rpc = vi.fn(async (method, params) => {
      if (method === "list-agent-profiles") return { profiles: [projectProfile] };
      if (method === "get-agent-profile" && params.scope === "project") return { profile: projectProfile, yaml: "name: reviewer-v2\n", read_only: false };
      if (method === "get-agent-profile" && params.scope === "global") return { profile: globalProfile, yaml: "name: reviewer-v2\n", read_only: false };
      if (method === "save-agent-profile") throw conflict;
      throw new Error(`unexpected ${method}`);
    });
    initSettingsModal({ agentProfileRpc: rpc });
    renderSettingsModal({});
    profilesTab().click();
    await flush();
    document.querySelector<HTMLButtonElement>('[data-agent-profile="reviewer-v2"]')!.click();
    await flush();

    const scope = document.querySelector<HTMLSelectElement>("#agent-profile-scope")!;
    scope.value = "global";
    scope.dispatchEvent(new Event("change"));
    await flush();
    document.querySelector<HTMLButtonElement>("#agent-profile-save")!.click();
    await flush();

    expect(rpc).toHaveBeenCalledWith("save-agent-profile", expect.objectContaining({ scope: "global", expected_revision: 7 }));
    expect(document.querySelector("#agent-profile-diagnostics")!.textContent).toContain("revision 8");
    expect(document.querySelector("#agent-profile-diagnostics")!.textContent).toContain("global-hash-8");
  });

  it("makes bundled profiles read-only and disables save/delete", async () => {
    const rpc = vi.fn(async (method) => {
      if (method === "list-agent-profiles") return { profiles: [bundledProfile] };
      if (method === "get-agent-profile") return { profile: bundledProfile, yaml: "name: bundled-default\nrevision: 3\n", read_only: true };
      throw new Error(`unexpected ${method}`);
    });
    initSettingsModal({ agentProfileRpc: rpc });
    renderSettingsModal({});
    profilesTab().click();
    await flush();
    document.querySelector<HTMLButtonElement>('[data-agent-profile="bundled-default"]')!.click();
    await flush();

    expect(document.querySelector<HTMLTextAreaElement>("#agent-profile-yaml")!.readOnly).toBe(true);
    expect(document.querySelector<HTMLButtonElement>("#agent-profile-save")!.disabled).toBe(true);
    expect(document.querySelector<HTMLButtonElement>("#agent-profile-delete")!.disabled).toBe(true);
    expect(document.querySelector("#settings-content")!.textContent).toContain("只读");
  });

  it("deletes using the loaded content hash and surfaces revision/hash conflicts", async () => {
    const conflict = Object.assign(new Error("agent profile conflict"), {
      data: { current: { ...projectProfile, revision: 4, content_hash: "hash-current" } },
    });
    const rpc = vi.fn(async (method) => {
      if (method === "list-agent-profiles") return { profiles: [projectProfile] };
      if (method === "get-agent-profile") return { profile: projectProfile, yaml: "name: reviewer-v2\nrevision: 3\n", read_only: false };
      if (method === "save-agent-profile") throw conflict;
      if (method === "delete-agent-profile") return { ok: true };
      throw new Error(`unexpected ${method}`);
    });
    initSettingsModal({ agentProfileRpc: rpc });
    renderSettingsModal({});
    profilesTab().click();
    await flush();
    document.querySelector<HTMLButtonElement>('[data-agent-profile="reviewer-v2"]')!.click();
    await flush();

    document.querySelector<HTMLButtonElement>("#agent-profile-save")!.click();
    await flush();
    expect(document.querySelector("#agent-profile-diagnostics")!.textContent).toContain("revision 4");
    expect(document.querySelector("#agent-profile-diagnostics")!.textContent).toContain("hash-current");

    document.querySelector<HTMLButtonElement>("#agent-profile-delete")!.click();
    await flush();
    expect(rpc).toHaveBeenCalledWith("delete-agent-profile", {
      scope: "project", name: "reviewer-v2", expected_hash: "hash-3",
    });
  });
});
