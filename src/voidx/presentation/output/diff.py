"""Diff rendering helpers."""

from __future__ import annotations

import re

from rich.console import Console
from rich.markup import escape
from rich.syntax import Syntax

from voidx.platform.file_types import language_from_path
from voidx.tooling.adapters.git_diff import git_diff, git_diff_stat
from voidx.tooling.domain.diff import (
    DiffLine,
    FileDiff,
    make_file_diff,
    parse_unified_diff,
)


def render_diff(console: Console, diff_text: str, title: str = "") -> None:
    """Render a unified diff with syntax highlighting."""
    if not diff_text.strip():
        return
    if title:
        console.print(f"[bold]{title}[/bold]")
    console.print(Syntax(diff_text, "diff", theme="monokai", line_numbers=False))


def render_file_change_lines(file_diff: FileDiff, max_hunks: int = 1, max_lines: int = 8) -> tuple[list[str], bool]:
    lines = [_summary_line(file_diff)]
    omitted = False
    shown_hunks = 0
    shown_lines = 0
    language = language_from_path(file_diff.path)

    for hunk in file_diff.hunks:
        if shown_hunks >= max_hunks:
            omitted = True
            break
        for line in hunk.lines:
            if shown_lines >= max_lines:
                omitted = True
                break
            lines.append(_render_diff_line(line, language))
            shown_lines += 1
        shown_hunks += 1
        if omitted:
            break

    return lines, omitted


def render_full_file_diff_lines(file_diff: FileDiff) -> list[str]:
    lines = [_summary_line(file_diff)]
    language = language_from_path(file_diff.path)
    for hunk in file_diff.hunks:
        if hunk.section:
            lines.append(f"[dim]@@ {escape(hunk.section)}[/dim]")
        for line in hunk.lines:
            lines.append(_render_diff_line(line, language))
    return lines


def _summary_line(file_diff: FileDiff) -> str:
    added = f"Added {file_diff.added} line{'s' if file_diff.added != 1 else ''}"
    removed = f"removed {file_diff.removed} line{'s' if file_diff.removed != 1 else ''}"
    return f"[dim]└[/dim]  {added}, {removed}"


def _render_diff_line(line: DiffLine, language: str = "") -> str:
    lineno = line.old_lineno if line.kind == "remove" else line.new_lineno
    number = "" if lineno is None else str(lineno)
    prefix = f"{number:>5} "
    text = _highlight_code(line.text, language)
    if line.kind == "add":
        return f"[on #003b0a][#A3BE8C]{prefix}+[/#A3BE8C]  {text}[/on #003b0a]"
    if line.kind == "remove":
        return f"[on #4a0000][#BF616A]{prefix}-[/#BF616A]  {text}[/on #4a0000]"
    return f"[dim]{prefix}[/dim]   {text}"


_TOKEN_RE = re.compile(
    r"(?P<comment>//.*|#.*)"
    r"|(?P<string>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')"
    r"|(?P<number>\b\d+(?:\.\d+)?\b)"
    r"|(?P<word>\b[A-Za-z_][A-Za-z0-9_]*\b)"
)

_COMMON_KEYWORDS = {
    "and", "as", "async", "await", "break", "case", "catch", "class", "const",
    "continue", "def", "default", "delete", "do", "else", "enum", "except",
    "export", "false", "final", "finally", "for", "from", "func", "function",
    "if", "impl", "import", "in", "interface", "let", "match", "namespace",
    "new", "none", "nullptr", "package", "private", "protected", "public",
    "return", "self", "static", "struct", "switch", "this", "throw", "true",
    "try", "type", "using", "var", "void", "while",
}


def _highlight_code(text: str, language: str) -> str:
    if not text:
        return ""
    if language == "markdown":
        return escape(text)

    rendered: list[str] = []
    last = 0
    for match in _TOKEN_RE.finditer(text):
        if match.start() > last:
            rendered.append(escape(text[last:match.start()]))
        token = match.group(0)
        kind = match.lastgroup or ""
        if kind == "comment":
            rendered.append(f"[#7A7F8A]{escape(token)}[/#7A7F8A]")
        elif kind == "string":
            rendered.append(f"[#EBCB8B]{escape(token)}[/#EBCB8B]")
        elif kind == "number":
            rendered.append(f"[#B48EFD]{escape(token)}[/#B48EFD]")
        elif kind == "word" and token.lower() in _COMMON_KEYWORDS:
            rendered.append(f"[#ff5caa]{escape(token)}[/#ff5caa]")
        else:
            rendered.append(escape(token))
        last = match.end()
    if last < len(text):
        rendered.append(escape(text[last:]))
    return "".join(rendered)
