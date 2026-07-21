// @ts-nocheck
import { describe, it, expect, beforeEach } from "vitest";
import {
  setCommandCatalog,
  matchSlashCommands,
  isKnownSlashCommand,
  completeSlashInput,
  _resetCommandCatalogForTest,
} from "../../src/ui/slash";

const REMOTE = [
  { command: "/mcp", description: "Manage MCP servers", category: "integrations", execution: "open-ui", dangerous: false, requiresArgs: false, uiTarget: "integrations:mcp" },
  { command: "/mcp new", description: "Add an MCP server", category: "integrations", execution: "fill", dangerous: false, requiresArgs: true },
  { command: "/mcp del", description: "Remove an MCP server", category: "integrations", execution: "fill", dangerous: true, requiresArgs: false },
  { command: "/compact", description: "Compact context", category: "maintenance", execution: "fill", dangerous: false, requiresArgs: false },
  { command: "/usage", description: "Show token usage", category: "maintenance", execution: "run", dangerous: false, requiresArgs: false },
  { command: "/zz-remote-only", description: "Remote-only command", category: "maintenance", execution: "fill", dangerous: false, requiresArgs: false },
];

beforeEach(() => {
  _resetCommandCatalogForTest();
});

describe("setCommandCatalog", () => {
  it("replaces the catalog so matching uses remote data", () => {
    setCommandCatalog(REMOTE);
    expect(isKnownSlashCommand("/zz-remote-only")).toBe(true);
    expect(matchSlashCommands("/zz").map((c) => c.command)).toEqual(["/zz-remote-only"]);
  });

  it("keeps subcommand matching for remote catalog", () => {
    setCommandCatalog(REMOTE);
    const matched = matchSlashCommands("/mcp ");
    expect(matched.map((c) => c.command)).toEqual(["/mcp new", "/mcp del"]);
  });

  it("falls back to builtin catalog after reset", () => {
    setCommandCatalog(REMOTE);
    _resetCommandCatalogForTest();
    expect(isKnownSlashCommand("/zz-remote-only")).toBe(false);
    expect(matchSlashCommands("/m").length).toBeGreaterThan(0);
  });

  it("ignores empty remote list and keeps builtin", () => {
    setCommandCatalog([]);
    expect(matchSlashCommands("/m").length).toBeGreaterThan(0);
  });
});

describe("completeSlashInput", () => {
  beforeEach(() => {
    setCommandCatalog(REMOTE);
  });

  it("completes a unique prefix directly", () => {
    expect(completeSlashInput("/usa")).toBe("/usage ");
  });

  it("extends to the common prefix for multiple matches", () => {
    expect(completeSlashInput("/mcp ")).toBe("/mcp ");
    expect(completeSlashInput("/mcp n")).toBe("/mcp new ");
  });

  it("extends partial input to shared prefix when several commands match", () => {
    // /mcp matches /mcp, /mcp new, /mcp del — common extension is "/mcp "
    expect(completeSlashInput("/mcp")).toBe("/mcp ");
  });

  it("returns null when nothing matches", () => {
    expect(completeSlashInput("/xyz")).toBe(null);
  });

  it("returns null for non-slash input", () => {
    expect(completeSlashInput("hello")).toBe(null);
  });
});
