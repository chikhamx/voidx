"""Startup screen — Claude Code style terminal UI."""

from __future__ import annotations

from typing import Protocol

from rich.cells import cell_len
from rich.markup import escape


class StartupConsole(Protocol):
    width: int

    def print(self, *args, **kwargs) -> None: ...


def show_startup(
    console: StartupConsole,
    model: str,
    provider: str,
    workspace: str,
    session_title: str,
    is_new: bool,
) -> None:
    """Render the Claude Code style startup banner."""

    console.print("\n".join(render_startup_lines(
        console.width,
        model=model,
        provider=provider,
        workspace=workspace,
        session_title=session_title,
        is_new=is_new,
    )))


def render_startup_lines(
    console_width: int,
    *,
    model: str,
    provider: str,
    workspace: str,
    session_title: str,
    is_new: bool,
) -> list[str]:
    """Build Rich-markup startup banner lines."""

    from voidx import __version__
    import os

    folder_name = os.path.basename(workspace) or workspace

    greeting = "Welcome back!" if not is_new else "Welcome to voidx!"
    session_line = "New session" if is_new else f"Resumed: {session_title or 'previous session'}"
    info: list[tuple[str, str]] = [
        ("greeting", greeting),
        ("model", f"● Model  {provider}/{model}"),
        ("folder", f"▣ Workspace  {folder_name}"),
        ("session", f"↳ {session_line}"),
        ("hint", "Ask anything, or type / for commands"),
        ("hint", "/model switch · /diff · wheel scrolls"),
    ]

    title = f"voidx v{__version__}"
    logo = _cat_logo()
    logo_plain = [plain for plain, _ in logo]
    width = _banner_width(console_width, title, logo_plain, [line for _, line in info])
    logo_width = max(cell_len(line) for line in logo_plain)
    info_width = max(width - logo_width - 4, 16)

    rendered: list[str] = []
    title_gap = max(width - cell_len(title) - 1, 0)
    rendered.append(f"[dim]╭─ {escape(title)} {'─' * title_gap}╮[/dim]")
    for idx in range(max(len(logo), len(info))):
        left, styled_left = logo[idx] if idx < len(logo) else ("", "")
        item = info[idx] if idx < len(info) else ("", "")
        right = _fit_cell(item[1], info_width)
        plain = f"{left}{' ' * (logo_width - cell_len(left) + 4)}{right}"
        pad = max(width - cell_len(plain), 0)
        rendered.append(
            f"[dim]│[/dim] {styled_left}"
            f"{' ' * (logo_width - cell_len(left) + 4)}"
            f"{_style_info(item[0], right)}"
            f"{' ' * pad} [dim]│[/dim]"
        )
    rendered.append(f"[dim]╰{'─' * (width + 2)}╯[/dim]")

    return rendered


def _cat_logo() -> list[tuple[str, str]]:
    outline = "#8B6F62"
    eye = "#F2D6A2"
    bubble = "#C9B79A"
    return [
        (
            "       o     O        ",
            f"[{bubble}]       o     O        [/]",
        ),
        (
            "    /\\________/\\    ╭╮",
            f"[{outline}]    /\\________/\\    ╭╮[/]",
        ),
        (
            "   /  ◒      ◒  \\___││",
            f"[{outline}]   /  [/][{eye}]◒      ◒[/][{outline}]  \\___││[/]",
        ),
        (
            "  |   ▔      ▔      \\││",
            f"[{outline}]  |   [/][{eye}]▔      ▔[/][{outline}]      \\││[/]",
        ),
        (
            "  |                __╰╯",
            f"[{outline}]  |                __╰╯[/]",
        ),
        (
            "   \\______________/    ",
            f"[{outline}]   \\______________/    [/]",
        ),
    ]


def _banner_width(console_width: int, title: str, logo: list[str], info: list[str]) -> int:
    logo_width = max(cell_len(line) for line in logo)
    info_width = max(cell_len(line) for line in info)
    content_width = max(logo_width + 4 + info_width, cell_len(title) + 4)
    return min(content_width, max(console_width - 4, 44))


def _fit_cell(text: str, width: int) -> str:
    if cell_len(text) <= width:
        return text
    if width <= 3:
        return "." * max(width, 0)
    out = ""
    used = 0
    for char in text:
        char_width = cell_len(char)
        if used + char_width > width - 3:
            break
        out += char
        used += char_width
    return out + "..."


def _style_info(kind: str, text: str) -> str:
    escaped = escape(text)
    if kind == "greeting":
        return f"[bold white]{escaped}[/bold white]"
    if kind == "model":
        if text.startswith("● Model  "):
            return f"[#A3BE8C]●[/#A3BE8C] [dim]Model  [/dim][bold]{escape(text[9:])}[/bold]"
        return f"[bold]{escaped}[/bold]"
    if kind == "folder":
        prefix = "▣ Workspace  "
        if text.startswith(prefix):
            return f"[dim]▣ Workspace  [/dim][cyan]{escape(text[len(prefix):])}[/cyan]"
    if kind == "session":
        if text.startswith("↳ "):
            return f"[dim]↳ [/dim][cyan]{escape(text[2:])}[/cyan]"
    return f"[dim]{escaped}[/dim]"
