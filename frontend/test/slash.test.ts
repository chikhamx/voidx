// @ts-nocheck
import { describe, it, expect } from "vitest";
import { matchSlashCommands, renderSlashMenu } from "../src/slash";

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

  it("matches /m prefix to /mcp, /mode, and /model", () => {
    const result = matchSlashCommands("/m");
    expect(result).toHaveLength(3);
    expect(result[0].command).toBe("/mcp");
    expect(result[1].command).toBe("/mode");
    expect(result[2].command).toBe("/model");
  });

  it("matches exact /mcp", () => {
    const result = matchSlashCommands("/mcp");
    expect(result).toHaveLength(1);
    expect(result[0].command).toBe("/mcp");
  });

  it("matches /s prefix to session, sandbox, and skills commands", () => {
    const result = matchSlashCommands("/s");
    expect(result.map((c) => c.command)).toEqual(["/sandbox", "/session", "/skills"]);
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


  it("matches commands by description text", () => {
    const result = matchSlashCommands("/approval");
    expect(result.map((c) => c.command)).toContain("/approval");
    expect(result[0].category).toBe("permission");
  });

  it("includes metadata for open-ui and dangerous commands", () => {
    const modelNew = matchSlashCommands("/model new")[0];
    expect(modelNew.execution).toBe("open-ui");
    expect(modelNew.uiTarget).toBe("settings:model");
    const rollback = matchSlashCommands("/rollback")[0];
    expect(rollback.dangerous).toBe(true);
    expect(rollback.category).toBe("maintenance");
  });

describe("matchSlashCommands subcommands", () => {
  it("lists /model subcommands when input is '/model '", () => {
    const result = matchSlashCommands("/model ");
    const cmds = result.map((c) => c.command);
    expect(cmds).toContain("/model ctx");
    expect(cmds).toContain("/model reasoning");
    expect(cmds).toContain("/model new");
  });

  it("filters /model subcommands by prefix", () => {
    const result = matchSlashCommands("/model c");
    expect(result).toHaveLength(1);
    expect(result[0].command).toBe("/model ctx");
  });

  it("filters /model subcommands case-insensitively", () => {
    const result = matchSlashCommands("/model CTX");
    expect(result).toHaveLength(1);
    expect(result[0].command).toBe("/model ctx");
  });

  it("matches /model r to reasoning", () => {
    const result = matchSlashCommands("/model r");
    expect(result).toHaveLength(1);
    expect(result[0].command).toBe("/model reasoning");
  });

  it("returns empty when subcommand prefix matches nothing", () => {
    expect(matchSlashCommands("/model zzz")).toEqual([]);
  });

  it("still matches top-level /model without trailing space", () => {
    const result = matchSlashCommands("/model");
    expect(result).toHaveLength(1);
    expect(result[0].command).toBe("/model");
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
