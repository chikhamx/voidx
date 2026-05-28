"""Diff rendering — unified diff with Rich Syntax green/red coloring."""

from __future__ import annotations

import difflib
import subprocess
from pathlib import Path

from rich.console import Console
from rich.syntax import Syntax


def render_diff(console: Console, diff_text: str, title: str = "") -> None:
    """Render a unified diff with syntax highlighting."""
    if not diff_text.strip():
        return
    if title:
        console.print(f"[bold]{title}[/bold]")
    console.print(Syntax(diff_text, "diff", theme="monokai", line_numbers=False))


def make_file_diff(
    filepath: str,
    old_content: str,
    new_content: str,
    old_label: str = "",
    new_label: str = "",
) -> str:
    """Generate a unified diff between old and new content."""
    old = old_content.splitlines(keepends=True)
    new = new_content.splitlines(keepends=True)
    a = old_label or f"a/{filepath}"
    b = new_label or f"b/{filepath}"
    diff = difflib.unified_diff(old, new, fromfile=a, tofile=b)
    return "".join(diff)


def diff_stat(diff_text: str) -> tuple[int, int]:
    """Return (added, removed) line counts from a unified diff."""
    added = 0
    removed = 0
    for line in diff_text.split("\n"):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


def git_diff(workspace: str, staged: bool = False) -> str:
    """Get working tree diff via git."""
    try:
        args = ["git", "diff"]
        if staged:
            args.append("--staged")
        result = subprocess.run(
            args, capture_output=True, text=True,
            cwd=workspace, timeout=10,
        )
        return result.stdout
    except Exception:
        return ""


def git_diff_stat(workspace: str) -> str:
    """Get git diff --stat summary."""
    try:
        result = subprocess.run(
            ["git", "diff", "--stat"],
            capture_output=True, text=True,
            cwd=workspace, timeout=10,
        )
        return result.stdout.strip()
    except Exception:
        return ""
