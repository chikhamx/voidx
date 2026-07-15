import type { SlashCommand } from "../utils/types";

export const COMMAND_CATALOG: SlashCommand[] = [
  { command: "/allow", description: "Allow a tool for this session", category: "permission", execution: "fill", dangerous: false, requiresArgs: true },
  { command: "/clear", description: "Start a new session with empty context", category: "session", execution: "run", dangerous: true, requiresArgs: false },
  { command: "/code-ide", description: "Choose app for opening changed files", category: "code", execution: "open-ui", dangerous: false, requiresArgs: false, uiTarget: "settings:code" },
  { command: "/compact", description: "Manually trigger context compaction", category: "maintenance", execution: "fill", dangerous: false, requiresArgs: false },
  { command: "/debug", description: "Toggle verbose step/tool output", category: "maintenance", execution: "fill", dangerous: false, requiresArgs: false },
  { command: "/deny", description: "Deny a tool for this session", category: "permission", execution: "fill", dangerous: false, requiresArgs: true },
  { command: "/diff", description: "Show git working tree diff", category: "code", execution: "fill", dangerous: false, requiresArgs: false },
  { command: "/exit", description: "Exit voidx", category: "session", execution: "run", dangerous: false, requiresArgs: false },
  { command: "/goal", description: "Set or show current goal", category: "permission", execution: "fill", dangerous: false, requiresArgs: true },
  { command: "/guide", description: "Add guidance to the running agent turn", category: "permission", execution: "fill", dangerous: false, requiresArgs: true },
  { command: "/help", description: "Show all commands", category: "maintenance", execution: "run", dangerous: false, requiresArgs: false },
  { command: "/init", description: "Initialize project config", category: "maintenance", execution: "fill", dangerous: false, requiresArgs: false },
  { command: "/lang", description: "Set response language preference", category: "preference", execution: "fill", dangerous: false, requiresArgs: true },
  { command: "/list", description: "List saved sessions", category: "session", execution: "run", dangerous: false, requiresArgs: false },
  { command: "/log", description: "Toggle LLM logging", category: "maintenance", execution: "fill", dangerous: false, requiresArgs: false },
  { command: "/loop", description: "Run a prompt on a recurring interval", category: "maintenance", execution: "fill", dangerous: false, requiresArgs: true },
  { command: "/loop stop", description: "Stop the current loop", category: "maintenance", execution: "fill", dangerous: false, requiresArgs: false },
  { command: "/loop status", description: "Show current loop status", category: "maintenance", execution: "fill", dangerous: false, requiresArgs: false },
  { command: "/lsp", description: "Manage language servers", category: "code", execution: "open-ui", dangerous: false, requiresArgs: false, uiTarget: "integrations:lsp" },
  { command: "/mcp", description: "Manage MCP servers", category: "integrations", execution: "open-ui", dangerous: false, requiresArgs: false, uiTarget: "integrations:mcp" },
  { command: "/mode", description: "Choose interaction mode: auto|plan|goal", category: "permission", execution: "fill", dangerous: false, requiresArgs: false },
  { command: "/model", description: "Switch model or adjust reasoning", category: "model", execution: "fill", dangerous: false, requiresArgs: false },
  { command: "/parallel", description: "Toggle parallel subagent execution", category: "preference", execution: "fill", dangerous: false, requiresArgs: false },
  { command: "/paste", description: "Paste an image from the clipboard", category: "code", execution: "run", dangerous: false, requiresArgs: false },
  { command: "/permission", description: "Choose permission preset", category: "permission", execution: "open-ui", dangerous: false, requiresArgs: false, uiTarget: "settings:permissions" },
  { command: "/permissions", description: "Show current permission rules", category: "permission", execution: "open-ui", dangerous: false, requiresArgs: false, uiTarget: "settings:permissions" },
  { command: "/plan", description: "Enter plan mode", category: "permission", execution: "run", dangerous: false, requiresArgs: false },
  { command: "/quit", description: "Exit voidx", category: "session", execution: "run", dangerous: false, requiresArgs: false },
  { command: "/resume", description: "Resume a session", category: "session", execution: "fill", dangerous: false, requiresArgs: true },
  { command: "/rollback", description: "Revert file changes from the current turn", category: "maintenance", execution: "run", dangerous: true, requiresArgs: false },
  { command: "/session", description: "Session management", category: "session", execution: "fill", dangerous: false, requiresArgs: false },
  { command: "/skills", description: "Skill management", category: "integrations", execution: "open-ui", dangerous: false, requiresArgs: false, uiTarget: "integrations:skills" },
  { command: "/tavily", description: "Configure Tavily API key for web search", category: "integrations", execution: "open-ui", dangerous: false, requiresArgs: false, uiTarget: "integrations:web-search" },
  { command: "/title", description: "Set session title", category: "session", execution: "fill", dangerous: false, requiresArgs: true },
  { command: "/tone", description: "Set response tone preference", category: "preference", execution: "fill", dangerous: false, requiresArgs: true },
  { command: "/unplan", description: "Return to auto mode", category: "permission", execution: "run", dangerous: false, requiresArgs: false },
  { command: "/upgrade", description: "Check for voidx updates", category: "maintenance", execution: "fill", dangerous: false, requiresArgs: false },
  { command: "/usage", description: "Show token usage for this session", category: "maintenance", execution: "run", dangerous: false, requiresArgs: false },
  { command: "/model ctx", description: "Set context window size", category: "model", execution: "open-ui", dangerous: false, requiresArgs: false, uiTarget: "settings:model" },
  { command: "/model del", description: "Remove a profile", category: "model", execution: "fill", dangerous: true, requiresArgs: false },
  { command: "/model list", description: "Show configured model details", category: "model", execution: "open-ui", dangerous: false, requiresArgs: false, uiTarget: "settings:model" },
  { command: "/model new", description: "Create or update a model profile", category: "model", execution: "open-ui", dangerous: false, requiresArgs: false, uiTarget: "settings:model" },
  { command: "/model reasoning", description: "Set reasoning effort level", category: "model", execution: "open-ui", dangerous: false, requiresArgs: false, uiTarget: "settings:model" },
  { command: "/model switch", description: "Switch to a configured provider", category: "model", execution: "fill", dangerous: false, requiresArgs: true },
  { command: "/model test", description: "Test a provider's connectivity", category: "model", execution: "open-ui", dangerous: false, requiresArgs: false, uiTarget: "settings:model" },
];

export function isKnownSlashCommand(input: string): boolean {
  if (!input || !input.startsWith("/")) {
    return false;
  }
  const head = input.trim().split(/\s+/)[0].toLowerCase();
  return COMMAND_CATALOG.some((cmd) => cmd.command.toLowerCase() === head);
}

export function matchSlashCommands(input: string): SlashCommand[] {
  if (!input || !input.startsWith("/")) {
    return [];
  }
  const rawQuery = input.toLowerCase();
  const query = rawQuery.trimEnd();
  const wantsSubcommands = rawQuery.endsWith(" ") || query.includes(" ");
  if (!wantsSubcommands) {
    const topLevel = COMMAND_CATALOG.filter((cmd) => !cmd.command.includes(" "));
    const exact = topLevel.filter((cmd) => cmd.command.toLowerCase() === query);
    if (exact.length > 0) {
      return exact;
    }
    const descriptionQuery = query.slice(1);
    return topLevel.filter((cmd) => {
      const command = cmd.command.toLowerCase();
      const description = cmd.description.toLowerCase();
      return (
        command.startsWith(query) ||
        (descriptionQuery.length > 2 && description.includes(descriptionQuery))
      );
    });
  }
  const commandQuery = rawQuery.endsWith(" ") ? `${query} ` : query;
  const descriptionQuery = query.slice(1);
  const matched = COMMAND_CATALOG.filter((cmd) => {
    const command = cmd.command.toLowerCase();
    const description = cmd.description.toLowerCase();
    return (
      command.startsWith(commandQuery) ||
      (descriptionQuery.length > 2 && description.includes(descriptionQuery))
    );
  });
  return matched.length > 0 ? matched : [];
}

export function renderSlashMenu(
  commands: SlashCommand[],
  selectedIndex: number,
  onSelect?: (cmd: SlashCommand) => void,
): HTMLElement {
  const menu = document.createElement("div");
  menu.className = "slash-menu";
  if (!commands.length) {
    return menu;
  }
  commands.forEach((cmd, index) => {
    const item = document.createElement("div");
    item.className = "slash-item";
    item.dataset.command = cmd.command;
    item.dataset.execution = cmd.execution || "fill";
    if (cmd.uiTarget) item.dataset.uiTarget = cmd.uiTarget;
    if (cmd.category) item.dataset.category = cmd.category;
    if (cmd.dangerous) item.classList.add("dangerous");
    if (index === selectedIndex) {
      item.classList.add("selected");
    }
    if (typeof onSelect === "function") {
      item.addEventListener("click", () => onSelect(cmd));
    }
    const commandEl = document.createElement("span");
    commandEl.className = "slash-command";
    commandEl.textContent = cmd.command;
    const metaEl = document.createElement("span");
    metaEl.className = "slash-meta";
    metaEl.textContent = cmd.category || "command";
    const descEl = document.createElement("span");
    descEl.className = "slash-desc";
    descEl.textContent = cmd.description;
    item.append(commandEl, metaEl, descEl);
    menu.append(item);
  });
  return menu;
}
