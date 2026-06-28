const SLASH_COMMANDS = [
  { command: "/mcp", description: "Manage MCP servers" },
  { command: "/model", description: "Switch model or adjust reasoning" },
  { command: "/lsp", description: "Language server operations" },
  { command: "/session", description: "Session management" },
  { command: "/skills", description: "Skill management" },
  { command: "/init", description: "Initialize project config" },
];

export function matchSlashCommands(input) {
  if (!input || !input.startsWith("/")) {
    return [];
  }
  const query = input.toLowerCase();
  const matched = SLASH_COMMANDS.filter((cmd) =>
    cmd.command.toLowerCase().startsWith(query),
  );
  return matched.length > 0 ? matched : [];
}

export function renderSlashMenu(commands, selectedIndex) {
  const menu = document.createElement("div");
  menu.className = "slash-menu";
  if (!commands.length) {
    return menu;
  }
  commands.forEach((cmd, index) => {
    const item = document.createElement("div");
    item.className = "slash-item";
    if (index === selectedIndex) {
      item.classList.add("selected");
    }
    const commandEl = document.createElement("span");
    commandEl.className = "slash-command";
    commandEl.textContent = cmd.command;
    const descEl = document.createElement("span");
    descEl.className = "slash-desc";
    descEl.textContent = cmd.description;
    item.append(commandEl, descEl);
    menu.append(item);
  });
  return menu;
}
