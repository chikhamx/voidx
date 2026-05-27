"""Startup screen — Claude Code style terminal UI."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table
from rich.text import Text


def show_startup(
    console: Console,
    model: str,
    provider: str,
    workspace: str,
    session_title: str,
    is_new: bool,
) -> None:
    """Render the Claude Code style startup banner."""

    greeting = "Welcome!" if is_new else "Welcome back!"
    model_line = f"{provider}/{model}"

    # Build the header box with a grid table
    header = Table.grid(padding=(0, 2))
    header.add_column(ratio=2)
    header.add_column(ratio=1)

    left = Text()
    left.append(f"  {greeting}", style="bold")
    left.append("\n")
    left.append(f"  Tips: /help for commands, /list for sessions", style="dim")

    right = Text()
    right.append(f"{model_line}\n", style="bold")
    right.append(f"{workspace}", style="dim")

    header.add_row(left, right)

    console.print("")
    console.print(header)
    console.print("─" * console.width, style="dim")
    console.print(
        f'  [dim]Try "what can voidx help me with?"[/dim]'
    )
    console.print("─" * console.width, style="dim")
    console.print("  Press Ctrl-C again to exit")
    console.print("")
