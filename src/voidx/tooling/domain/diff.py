"""Pure diff parsing and generation helpers."""

from __future__ import annotations

import difflib
import re
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


def render_numbered_diff(file_diff: FileDiff) -> str:
    """Render a FileDiff as unified-diff-like text with per-line numbers."""
    if not file_diff.hunks:
        return ""

    lines = [f"--- {file_diff.old_path}", f"+++ {file_diff.new_path}"]
    for hunk in file_diff.hunks:
        section = f" {hunk.section}" if hunk.section else ""
        lines.append(f"@@ -{hunk.old_start},{hunk.old_count} +{hunk.new_start},{hunk.new_count} @@{section}")
        for line in hunk.lines:
            if line.kind == "remove":
                marker = "-"
                lineno = line.old_lineno
            elif line.kind == "add":
                marker = "+"
                lineno = line.new_lineno
            else:
                marker = " "
                lineno = line.new_lineno
            if lineno is not None:
                lines.append(f"{marker}{lineno}\t{line.text}")
    return "\n".join(lines) + "\n"


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


def make_structured_diff(
    filepath: str,
    old_content: str,
    new_content: str,
) -> FileDiff:
    """Generate a structured FileDiff directly, without a text round-trip.

    Uses SequenceMatcher.get_grouped_opcodes(n=3) to match unified_diff's
    hunk-grouping behavior.  Handles the old_start=0 / new_start=0 convention
    for pure-insert / pure-delete hunks.
    """
    old = old_content.splitlines(keepends=True)
    new = new_content.splitlines(keepends=True)
    sm = difflib.SequenceMatcher(a=old, b=new, autojunk=False)

    file_diff = FileDiff(
        old_path=f"a/{filepath}",
        new_path=f"b/{filepath}",
        path=filepath,
        operation="Update",
    )
    if not old and not new:
        return file_diff
    if not old:
        file_diff.operation = "Create"
    elif not new:
        file_diff.operation = "Delete"

    for group in sm.get_grouped_opcodes(n=3):
        first_tag, first_i1, first_i2, first_j1, first_j2 = group[0]
        last_tag, last_i1, last_i2, last_j1, last_j2 = group[-1]

        old_count = last_i2 - first_i1
        new_count = last_j2 - first_j1
        old_start = 0 if old_count == 0 else first_i1 + 1
        new_start = 0 if new_count == 0 else first_j1 + 1

        hunk = DiffHunk(
            old_start=old_start,
            old_count=old_count,
            new_start=new_start,
            new_count=new_count,
        )

        old_lineno = old_start
        new_lineno = new_start
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for k in range(i2 - i1):
                    hunk.lines.append(DiffLine(
                        kind="context",
                        old_lineno=old_lineno,
                        new_lineno=new_lineno,
                        text=old[i1 + k].rstrip("\n"),
                    ))
                    old_lineno += 1
                    new_lineno += 1
            elif tag == "replace":
                for k in range(i2 - i1):
                    hunk.lines.append(DiffLine(
                        kind="remove",
                        old_lineno=old_lineno,
                        text=old[i1 + k].rstrip("\n"),
                    ))
                    old_lineno += 1
                for k in range(j2 - j1):
                    hunk.lines.append(DiffLine(
                        kind="add",
                        new_lineno=new_lineno,
                        text=new[j1 + k].rstrip("\n"),
                    ))
                    new_lineno += 1
            elif tag == "delete":
                for k in range(i2 - i1):
                    hunk.lines.append(DiffLine(
                        kind="remove",
                        old_lineno=old_lineno,
                        text=old[i1 + k].rstrip("\n"),
                    ))
                    old_lineno += 1
            elif tag == "insert":
                for k in range(j2 - j1):
                    hunk.lines.append(DiffLine(
                        kind="add",
                        new_lineno=new_lineno,
                        text=new[j1 + k].rstrip("\n"),
                    ))
                    new_lineno += 1

        file_diff.hunks.append(hunk)
        file_diff.added += sum(1 for line in hunk.lines if line.kind == "add")
        file_diff.removed += sum(1 for line in hunk.lines if line.kind == "remove")

    return file_diff


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
