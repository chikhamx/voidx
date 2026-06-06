"""Pure diff parsing and generation helpers."""

from __future__ import annotations

import difflib
import re
import subprocess
from typing import Literal

from pydantic import BaseModel, Field


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


_HUNK_RE = re.compile(
    r"@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@(?P<section>.*)"
)


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
            args,
            capture_output=True,
            text=True,
            cwd=workspace,
            timeout=10,
        )
        return result.stdout
    except Exception:
        return ""


def git_diff_stat(workspace: str) -> str:
    """Get git diff --stat summary."""
    try:
        result = subprocess.run(
            ["git", "diff", "--stat"],
            capture_output=True,
            text=True,
            cwd=workspace,
            timeout=10,
        )
        return result.stdout.strip()
    except Exception:
        return ""


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
