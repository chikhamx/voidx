import { describe, it, expect } from "vitest";
import { matchSlashCommands, renderSlashMenu } from "../src/slash.js";

describe("matchSlashCommands", () => {
  it("returns empty for empty input", () => {
    expect(matchSlashCommands("")).toEqual([]);
  });

  it("returns empty for input without leading slash", () => {
    expect(matchSlashCommands("hello")).toEqual([]);
  });

  it("returns empty for null/undefined", () => {
    expect(matchSlashCommands(null)).toEqual([]);
    expect(matchSlashCommands(undefined)).toEqual([]);
  });

  it("matches /m prefix to /mcp and /model", () => {
    const result = matchSlashCommands("/m");
    expect(result).toHaveLength(2);
    expect(result[0].command).toBe("/mcp");
    expect(result[1].command).toBe("/model");
  });

  it("matches exact /mcp", () => {
    const result = matchSlashCommands("/mcp");
    expect(result).toHaveLength(1);
    expect(result[0].command).toBe("/mcp");
  });

  it("matches /s prefix to /session and /skills", () => {
    const result = matchSlashCommands("/s");
    expect(result).toHaveLength(2);
    expect(result.map((c) => c.command)).toEqual(["/session", "/skills"]);
  });

  it("returns empty for unknown command", () => {
    expect(matchSlashCommands("/xyz")).toEqual([]);
  });

  it("is case-insensitive", () => {
    const result = matchSlashCommands("/MCP");
    expect(result).toHaveLength(1);
    expect(result[0].command).toBe("/mcp");
  });
});

describe("renderSlashMenu", () => {
  it("returns empty menu for empty commands", () => {
    const menu = renderSlashMenu([], 0);
    expect(menu.className).toBe("slash-menu");
    expect(menu.children).toHaveLength(0);
  });

  it("renders items with command and description", () => {
    const cmds = [
      { command: "/mcp", description: "Manage MCP servers" },
      { command: "/model", description: "Switch model" },
    ];
    const menu = renderSlashMenu(cmds, 0);
    expect(menu.children).toHaveLength(2);
    const first = menu.children[0];
    expect(first.className).toBe("slash-item selected");
    expect(first.querySelector(".slash-command").textContent).toBe("/mcp");
    expect(first.querySelector(".slash-desc").textContent).toBe("Manage MCP servers");
  });

  it("marks selected index", () => {
    const cmds = [
      { command: "/mcp", description: "a" },
      { command: "/model", description: "b" },
    ];
    const menu = renderSlashMenu(cmds, 1);
    expect(menu.children[0].className).toBe("slash-item");
    expect(menu.children[1].className).toBe("slash-item selected");
  });
});
