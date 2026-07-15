// @ts-nocheck
import { beforeEach, describe, expect, it } from "vitest";
import { initIntegrationsPanel, renderIntegrationsPanel, _resetIntegrationsForTest } from "../../src/ui/integrations";

beforeEach(() => {
  _resetIntegrationsForTest();
});

describe("initIntegrationsPanel", () => {
  it("captures DOM elements from integrations dialog", () => {
    initIntegrationsPanel();
    const dialog = document.querySelector("#integrations-dialog");
    expect(dialog).not.toBeNull();
  });
});

describe("renderIntegrationsPanel", () => {
  it("renders MCP servers section", () => {
    initIntegrationsPanel();
    renderIntegrationsPanel({
      mcp_servers: [
        { name: "tavily", transport: "stdio", disabled: false, tool_count: 3, command: null, url: null, tools: ["search", "fetch", "crawl"] },
        { name: "voidx-web", transport: "streamable-http", disabled: true, tool_count: 0, command: null, url: "http://localhost:3000", tools: [] },
      ],
      web_routes: { search: { server: "tavily", tool: "tavily_search" }, fetch: { server: "tavily", tool: "tavily_extract" } },
      tavily: { configured: true, source: "settings", masked_value: "tvly...abcd" },
      skills: [{ name: "react-patterns", scope: "project", enabled: true, auto: true, description: "React best practices" }],
      lsp: [{ language: "python", name: "pyright", status: "running" }],
    });

    const content = document.querySelector("#integrations-content");
    expect(content).not.toBeNull();
    const text = content.textContent;
    expect(text).toContain("tavily");
    expect(text).toContain("voidx-web");
    expect(text).toContain("configured");
    expect(text).toContain("react-patterns");
    expect(text).toContain("python");
  });

  it("renders action buttons for MCP rows", () => {
    initIntegrationsPanel();
    renderIntegrationsPanel({
      mcp_servers: [{ name: "demo", transport: "stdio", disabled: false, tool_count: 1, command: null, url: null, tools: ["alpha"] }],
      web_routes: { search: {}, fetch: {} },
      tavily: { configured: false, source: "none" },
      skills: [],
      lsp: [],
    });

    const content = document.querySelector("#integrations-content");
    const buttons = content.querySelectorAll(".integrations-btn");
    // Disable, Test, Tools, Restart, Delete, Set Key, Delete, LSP Doctor, LSP Restart All
    expect(buttons.length).toBeGreaterThanOrEqual(5);
  });

  it("shows empty state when no MCP servers", () => {
    initIntegrationsPanel();
    renderIntegrationsPanel({ mcp_servers: [], web_routes: {}, tavily: { configured: false, source: "none" }, skills: [], lsp: [] });

    const content = document.querySelector("#integrations-content");
    expect(content.textContent).toContain("No MCP servers configured");
    expect(content.textContent).toContain("No skills found");
    expect(content.textContent).toContain("No language servers");
  });
});
