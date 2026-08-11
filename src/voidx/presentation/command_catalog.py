"""Desktop command catalog metadata for slash commands."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from voidx.presentation.commands import COMMANDS

CommandCategory = Literal[
    "session",
    "model",
    "permission",
    "integrations",
    "code",
    "preference",
    "maintenance",
]
CommandExecution = Literal["fill", "run", "open-ui"]


@dataclass(frozen=True)
class CommandCatalogItem:
    command: str
    description: str
    category: CommandCategory
    execution: CommandExecution
    dangerous: bool = False
    requiresArgs: bool = False
    uiTarget: str | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        if data["uiTarget"] is None:
            data.pop("uiTarget")
        return data


CATEGORY_PREFIXES: tuple[tuple[tuple[str, ...], CommandCategory], ...] = (
    (("/session", "/clear", "/list", "/resume", "/title"), "session"),
    (("/model",), "model"),
    (("/permission", "/allow", "/deny", "/permissions", "/goal", "/plan", "/unplan"), "permission"),
    (("/mcp", "/tavily", "/skills"), "integrations"),
    (("/lsp", "/code-ide", "/diff", "/paste"), "code"),
    (("/lang", "/tone"), "preference"),
    (("/compact", "/debug", "/log", "/usage", "/upgrade", "/rollback"), "maintenance"),
)

OPEN_UI_TARGETS: dict[str, str] = {
    "/model new": "settings:model",
    "/model list": "settings:model",
    "/model test": "settings:model",
    "/model reasoning": "settings:model",
    "/model ctx": "settings:model",
    "/permission": "settings:permissions",
    "/permissions": "settings:permissions",
    "/code-ide": "settings:code",
    "/code-ide status": "settings:code",
    "/lsp status": "integrations:lsp",
    "/lsp doctor": "integrations:lsp",
    "/lsp restart": "integrations:lsp",
    "/mcp": "integrations:mcp",
    "/mcp list": "integrations:mcp",
    "/skills": "integrations:skills",
    "/skills list": "integrations:skills",
    "/tavily": "integrations:web-search",
    "/tavily show": "integrations:web-search",
}

DIRECT_RUN_COMMANDS = {
    "/usage",
    "/lsp status",
    "/tavily show",
    "/mcp list",
    "/skills list",
    "/code-ide status",
    "/session list",
    "/list",
}

DANGEROUS_COMMANDS = {
    "/rollback",
    "/clear",
    "/session del",
    "/mcp del",
    "/model del",
    "/permission full_access",
}

REQUIRES_ARGS = {
    "/allow",
    "/deny",
    "/guide",
    "/lang",
    "/model switch",
    "/mcp new",
    "/mcp test",
    "/mcp tools",
    "/mcp restart",
    "/mcp enable",
    "/mcp auto",
    "/mcp disable",
    "/mcp manual",
    "/session resume",
    "/resume",
    "/tavily set",
    "/bocha set",
    "/title",
    "/tone",
}


def command_category(command: str) -> CommandCategory:
    for prefixes, category in CATEGORY_PREFIXES:
        if any(command == prefix or command.startswith(prefix + " ") for prefix in prefixes):
            return category
    return "maintenance"


def command_execution(command: str) -> CommandExecution:
    if command in OPEN_UI_TARGETS:
        return "open-ui"
    if command in DIRECT_RUN_COMMANDS or command in DANGEROUS_COMMANDS:
        return "run"
    return "fill" if command in REQUIRES_ARGS or " " not in command else "fill"


def build_command_catalog() -> list[CommandCatalogItem]:
    catalog: list[CommandCatalogItem] = []
    for command, description in COMMANDS:
        ui_target = OPEN_UI_TARGETS.get(command)
        catalog.append(
            CommandCatalogItem(
                command=command,
                description=description,
                category=command_category(command),
                execution=command_execution(command),
                dangerous=command in DANGEROUS_COMMANDS,
                requiresArgs=command in REQUIRES_ARGS,
                uiTarget=ui_target,
            )
        )
    return catalog


def command_catalog_dicts() -> list[dict]:
    return [item.to_dict() for item in build_command_catalog()]


def find_command(text: str) -> CommandCatalogItem | None:
    value = text.strip().lower()
    if not value:
        return None
    matches = [item for item in build_command_catalog() if value == item.command.lower() or value.startswith(item.command.lower() + " ")]
    if not matches:
        return None
    return max(matches, key=lambda item: len(item.command))
