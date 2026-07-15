import { rpcCall } from '../rpc';

interface McpServer {
  name: string;
  transport?: string;
  disabled?: boolean;
  tool_count?: number;
}

interface Skill {
  name: string;
  enabled?: boolean;
  auto?: boolean;
  scope?: string;
  description?: string;
}

interface LspServer {
  language?: string;
  name?: string;
  status?: string;
}

interface TavilyConfig {
  configured?: boolean;
  source?: string;
}

export interface IntegrationsSnapshot {
  mcp_servers?: McpServer[];
  skills?: Skill[];
  lsp?: LspServer[];
  tavily?: TavilyConfig;
}

interface IntegrationsState {
  dialog: HTMLDialogElement | null;
  content: HTMLElement | null;
  close: HTMLButtonElement | null;
  error: HTMLElement | null;
}

let state: IntegrationsState = { dialog: null, content: null, close: null, error: null };

export function initIntegrationsPanel(): void {
  state.dialog = document.querySelector<HTMLDialogElement>("#integrations-dialog");
  state.content = document.querySelector<HTMLElement>("#integrations-content");
  state.close = document.querySelector<HTMLButtonElement>("#integrations-close");
  state.error = document.querySelector<HTMLElement>("#integrations-error");
  if (state.close) state.close.onclick = () => closeIntegrationsPanel();
}

export async function openIntegrationsPanel(snapshotPromise: Promise<IntegrationsSnapshot>): Promise<void> {
  if (!state.dialog) return;
  try {
    const snapshot = await snapshotPromise;
    renderIntegrationsPanel(snapshot);
    if (typeof state.dialog.showModal === "function") state.dialog.showModal();
    else state.dialog.setAttribute("open", "");
  } catch (error) {
    setError(error instanceof Error ? error.message : String(error));
  }
}

export function closeIntegrationsPanel(): void {
  if (!state.dialog) return;
  if (typeof state.dialog.close === "function") state.dialog.close();
  else state.dialog.removeAttribute("open");
}

export function renderIntegrationsPanel(snapshot: IntegrationsSnapshot = {}): void {
  if (!state.content) return;
  state.content.replaceChildren(
    section("MCP Servers", [
      ...(snapshot.mcp_servers && snapshot.mcp_servers.length
        ? snapshot.mcp_servers.map((server) => mcpRow(server))
        : [readonlyRow("Empty", "No MCP servers configured")]),
    ]),
    section("Web Search", [
      row("Tavily", tavilyDetail(snapshot.tavily), [
        btnInline("Set Key", async () => {
          const key = prompt("请输入 Tavily API Key (留空取消):");
          if (!key) return;
          try {
            await rpcCall("tavily.set", { api_key: key });
            refreshIntegrationsPanel();
          } catch (e) { alert((e as Error).message); }
        }),
        btnInline("Delete", async () => {
          if (!confirm("确认删除 Tavily API Key?")) return;
          try {
            await rpcCall("tavily.delete", {});
            refreshIntegrationsPanel();
          } catch (e) { alert((e as Error).message); }
        }),
      ]),
    ]),
    section("Skills", [
      ...(snapshot.skills && snapshot.skills.length
        ? snapshot.skills.map((skill) => skillRow(skill))
        : [readonlyRow("Empty", "No skills found")]),
    ]),
    section("Language Servers", [
      ...(snapshot.lsp && snapshot.lsp.length
        ? snapshot.lsp.map((server) => lspRow(server))
        : [readonlyRow("Empty", "No language servers")]),
      btnBar([
        btnInline("LSP Doctor", async () => {
          try {
            const result = await rpcCall("lsp.doctor", {}) as { checks: unknown };
            alert(JSON.stringify(result.checks, null, 2));
          } catch (e) { alert((e as Error).message); }
        }),
        btnInline("LSP Restart All", async () => {
          try {
            await rpcCall("lsp.restart", {});
            refreshIntegrationsPanel();
          } catch (e) { alert((e as Error).message); }
        }),
      ]),
    ]),
  );
}

export function _resetIntegrationsForTest(): void {
  state = { dialog: null, content: null, close: null, error: null };
}

async function refreshIntegrationsPanel(): Promise<void> {
  try {
    const snapshot = await rpcCall("integrations.get", {}) as IntegrationsSnapshot;
    renderIntegrationsPanel(snapshot);
  } catch { /* keep current */ }
}

// ── MCP server row ──────────────────────────────────────────────────────

function mcpRow(server: McpServer): HTMLDivElement {
  return row(server.name, `${server.transport || "stdio"} · ${server.disabled ? "disabled" : "enabled"} · ${server.tool_count || 0} tools`, [
    btnSmall(server.disabled ? "Enable" : "Disable", async () => {
      try {
        await rpcCall("mcp.setDisabled", { name: server.name, disabled: !server.disabled });
        refreshIntegrationsPanel();
      } catch (e) { alert((e as Error).message); }
    }),
    btnSmall("Test", async () => {
      try {
        const result = await rpcCall("mcp.test", { name: server.name }) as { message?: string };
        alert(result.message || "OK");
      } catch (e) { alert((e as Error).message); }
    }),
    btnSmall("Tools", async () => {
      try {
        const result = await rpcCall("mcp.tools", { name: server.name }) as { tools?: Array<{ name: string }> };
        const names = (result.tools || []).map((t) => t.name).join("\n");
        alert(names || "No tools listed");
      } catch (e) { alert((e as Error).message); }
    }),
    btnSmall("Restart", async () => {
      try {
        await rpcCall("mcp.restart", { name: server.name });
        refreshIntegrationsPanel();
      } catch (e) { alert((e as Error).message); }
    }),
    btnDanger("Delete", async () => {
      if (!confirm(`确认删除 MCP Server "${server.name}"?`)) return;
      try {
        await rpcCall("mcp.delete", { name: server.name, confirmed: true });
        refreshIntegrationsPanel();
      } catch (e) { alert((e as Error).message); }
    }),
  ]);
}

// ── skill row ───────────────────────────────────────────────────────────

function skillRow(skill: Skill): HTMLDivElement {
  return row(skill.name, `${skill.enabled ? "enabled" : "disabled"} · ${skill.auto ? "auto" : "manual"} · ${skill.scope} · ${skill.description || ""}`, [
    btnSmall(skill.enabled ? "Disable" : "Enable", async () => {
      try {
        await rpcCall("skills.setEnabled", { name: skill.name, enabled: !skill.enabled });
        refreshIntegrationsPanel();
      } catch (e) { alert((e as Error).message); }
    }),
    btnSmall(skill.auto ? "Manual" : "Auto", async () => {
      try {
        await rpcCall("skills.setAuto", { name: skill.name, auto: !skill.auto });
        refreshIntegrationsPanel();
      } catch (e) { alert((e as Error).message); }
    }),
  ]);
}

// ── LSP row ─────────────────────────────────────────────────────────────

function lspRow(server: LspServer): HTMLDivElement {
  return readonlyRow(server.language || server.name, `${server.status || "unknown"}`);
}

// ── helpers ─────────────────────────────────────────────────────────────

function section(title: string, children: HTMLElement[]): HTMLElement {
  const el = document.createElement("section");
  el.className = "settings-section integrations-section";
  const heading = document.createElement("h3");
  heading.textContent = title;
  el.append(heading, ...children);
  return el;
}

function row(name: string, detail: string, actions: HTMLElement[] = []): HTMLDivElement {
  const el = document.createElement("div");
  el.className = "settings-row integrations-row";
  const info = document.createElement("div");
  info.className = "integrations-info";
  const nameEl = document.createElement("span");
  nameEl.textContent = name;
  const detailEl = document.createElement("span");
  detailEl.className = "settings-readonly";
  detailEl.textContent = detail;
  info.append(nameEl, detailEl);
  el.append(info);
  if (actions.length) {
    const bar = document.createElement("div");
    bar.className = "integrations-actions";
    bar.append(...actions);
    el.append(bar);
  }
  return el;
}

function readonlyRow(label: string, value: string): HTMLDivElement {
  const el = document.createElement("div");
  el.className = "settings-row integrations-row";
  const info = document.createElement("div");
  info.className = "integrations-info";
  const labelEl = document.createElement("span");
  labelEl.textContent = label;
  const valueEl = document.createElement("span");
  valueEl.className = "settings-readonly";
  valueEl.textContent = value;
  info.append(labelEl, valueEl);
  el.append(info);
  return el;
}

function tavilyDetail(tavily: TavilyConfig = {}): string {
  return `${tavily.configured ? "configured" : "not configured"} · ${tavily.source || "none"}`;
}

function btnSmall(label: string, onClick: () => void): HTMLButtonElement {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "integrations-btn";
  btn.textContent = label;
  btn.onclick = onClick;
  return btn;
}

function btnDanger(label: string, onClick: () => void): HTMLButtonElement {
  const btn = btnSmall(label, onClick);
  btn.classList.add("integrations-btn-danger");
  return btn;
}

function btnInline(label: string, onClick: () => void): HTMLButtonElement {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "integrations-btn";
  btn.textContent = label;
  btn.onclick = onClick;
  return btn;
}

function btnBar(buttons: HTMLElement[]): HTMLDivElement {
  const bar = document.createElement("div");
  bar.className = "integrations-actions";
  bar.append(...buttons);
  return bar;
}

function setError(msg: string): void {
  if (state.error) state.error.textContent = msg;
}
