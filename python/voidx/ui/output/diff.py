"""Diff parsing and rendering helpers."""

from __future__ import annotations

import difflib
import re
import subprocess
from typing import Literal

from pydantic import BaseModel, Field
from rich.console import Console
from rich.markup import escape
from rich.syntax import Syntax


class DiffLine(BaseModel):
    kind: Literal["context", "add", "remove"]
    old_lineno: int | None = None
    new_lineno: int | None = None
    text: str = ""


class DiffHunk(BaseModel):
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    section: str = ""
    lines: list[DiffLine] = Field(default_factory=list)


class FileDiff(BaseModel):
    old_path: str = ""
    new_path: str = ""
    path: str = ""
    operation: Literal["Create", "Update", "Delete"] = "Update"
    added: int = 0
    removed: int = 0
    hunks: list[DiffHunk] = Field(default_factory=list)
    raw: str = ""


class StructuredDiff(BaseModel):
    files: list[FileDiff] = Field(default_factory=list)


def render_diff(console: Console, diff_text: str, title: str = "") -> None:
    """Render a unified diff with syntax highlighting."""
    if not diff_text.strip():
        return
    if title:
        console.print(f"[bold]{title}[/bold]")
    console.print(Syntax(diff_text, "diff", theme="monokai", line_numbers=False))


_HUNK_RE = re.compile(r"@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@(?P<section>.*)")


def parse_unified_diff(diff_text: str) -> StructuredDiff:
    files: list[FileDiff] = []
    current: FileDiff | None = None
    hunk: DiffHunk | None = None
    old_lineno = 0
    new_lineno = 0

    for raw in diff_text.splitlines():
        if raw.startswith("--- "):
            if current is not None:
                files.append(current)
            old_path = _clean_diff_path(raw[4:].strip())
            current = FileDiff(old_path=old_path, raw=raw)
            hunk = None
            continue

        if current is not None:
            current.raw = f"{current.raw}\n{raw}" if current.raw else raw

        if raw.startswith("+++ ") and current is not None:
            new_path = _clean_diff_path(raw[4:].strip())
            current.new_path = new_path
            current.path = _display_path(current.old_path, new_path)
            current.operation = _operation(current.old_path, new_path)
            continue

        match = _HUNK_RE.match(raw)
        if match and current is not None:
            hunk = DiffHunk(
                old_start=int(match.group("old_start")),
                old_count=int(match.group("old_count") or "1"),
                new_start=int(match.group("new_start")),
                new_count=int(match.group("new_count") or "1"),
                section=match.group("section").strip(),
            )
            current.hunks.append(hunk)
            old_lineno = hunk.old_start
            new_lineno = hunk.new_start
            continue

        if hunk is None or current is None:
            continue

        if raw.startswith("+") and not raw.startswith("+++"):
            hunk.lines.append(DiffLine(kind="add", new_lineno=new_lineno, text=raw[1:]))
            current.added += 1
            new_lineno += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            hunk.lines.append(DiffLine(kind="remove", old_lineno=old_lineno, text=raw[1:]))
            current.removed += 1
            old_lineno += 1
        elif raw.startswith(" "):
            hunk.lines.append(DiffLine(
                kind="context",
                old_lineno=old_lineno,
                new_lineno=new_lineno,
                text=raw[1:],
            ))
            old_lineno += 1
            new_lineno += 1
        elif raw.startswith("\\"):
            continue

    if current is not None:
        files.append(current)
    return StructuredDiff(files=files)


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


def language_from_path(path: str) -> str:
    suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    mapping = {
        "py": "python",
        "js": "javascript",
        "jsx": "javascript",
        "ts": "typescript",
        "tsx": "typescript",
        "cpp": "cpp",
        "cc": "cpp",
        "cxx": "cpp",
        "c": "c",
        "h": "cpp",
        "hpp": "cpp",
        "rs": "rust",
        "go": "go",
        "java": "java",
        "json": "json",
        "toml": "toml",
        "yaml": "yaml",
        "yml": "yaml",
        "md": "markdown",
        "css": "css",
        "html": "html",
    }
    return mapping.get(suffix, "")


def _summary_line(file_diff: FileDiff) -> str:
    added = f"Added {file_diff.added} line{'s' if file_diff.added != 1 else ''}"
    removed = f"removed {file_diff.removed} line{'s' if file_diff.removed != 1 else ''}"
    return f"[dim]└[/dim]  {added}, {removed}"


def _render_diff_line(line: DiffLine, language: str = "") -> str:
    lineno = line.new_lineno if line.kind == "add" else line.old_lineno
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


def _clean_diff_path(value: str) -> str:
    path = value.split("\t", 1)[0]
    if path in {"/dev/null", "dev/null"}:
        return "/dev/null"
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


def _display_path(old_path: str, new_path: str) -> str:
    if new_path and new_path != "/dev/null":
        return new_path
    return old_path


def _operation(old_path: str, new_path: str) -> Literal["Create", "Update", "Delete"]:
    if old_path == "/dev/null":
        return "Create"
    if new_path == "/dev/null":
        return "Delete"
    return "Update"


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
