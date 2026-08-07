"""Workspace file picker helpers for @ attachments."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "dist",
    "build",
}


@dataclass(frozen=True)
class AttachmentToken:
    start: int
    end: int
    query: str
    quoted: bool = False


@dataclass(frozen=True)
class FileCandidate:
    rel_path: str
    kind: str
    size: int
    mtime: float = 0.0

def find_attachment_token(text: str, cursor: int) -> AttachmentToken | None:
    cursor = max(0, min(cursor, len(text)))
    start = text.rfind("@", 0, cursor)
    while start != -1:
        if start == 0 or text[start - 1].isspace():
            break
        start = text.rfind("@", 0, start)
    if start == -1:
        return None
    if start + 1 < len(text) and text[start + 1] == '"':
        closing = text.find('"', start + 2)
        if closing != -1 and closing < cursor:
            return None
        return AttachmentToken(start=start, end=cursor, query=text[start + 2:cursor], quoted=True)
    token = text[start + 1:cursor]
    if any(ch.isspace() for ch in token):
        return None
    return AttachmentToken(start=start, end=cursor, query=token, quoted=False)


def list_file_candidates(workspace: str, query: str, limit: int = 8) -> list[FileCandidate]:
    """List candidates for @ attachment, scanning only one directory level.

    The query is interpreted as a path prefix: everything before the last ``/``
    is the directory to scan, and the part after is the filter.  For example:

    * ``@src``     → scan workspace root, filter by "src"
    * ``@src/``    → scan ``src/``, show all entries
    * ``@src/vo``  → scan ``src/``, filter by "vo"
    """
    root = Path(workspace).resolve()
    if not root.exists() or not root.is_dir():
        return []

    normalized_query = query.strip().replace("\\", "/")

    if "/" in normalized_query:
        dir_part, filter_part = normalized_query.rsplit("/", 1)
    else:
        dir_part = ""
        filter_part = normalized_query

    filter_lower = filter_part.lower()

    scan_dir = root / dir_part if dir_part else root
    if not scan_dir.is_dir():
        return []

    candidates: list[FileCandidate] = []
    try:
        entries = list(os.scandir(scan_dir))
    except (OSError, PermissionError):
        return []

    for entry in entries:
        name = entry.name
        if name.startswith("."):
            continue
        if entry.is_dir() and name in SKIP_DIRS:
            continue

        if filter_lower and not name.lower().startswith(filter_lower):
            continue

        try:
            mtime = entry.stat().st_mtime
        except OSError:
            mtime = 0.0

        rel_prefix = (dir_part + "/") if dir_part else ""
        if entry.is_dir():
            candidates.append(FileCandidate(
                rel_path=rel_prefix + name + "/",
                kind="dir",
                size=0,
                mtime=mtime,
            ))
        else:
            rel_path = rel_prefix + name
            candidates.append(FileCandidate(
                rel_path=rel_path,
                kind="image" if is_image_file(rel_path) else "file",
                size=0,
                mtime=mtime,
            ))

    candidates.sort(key=lambda item: -item.mtime)
    return candidates[:limit]


def is_image_file(path: str | Path) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXTENSIONS


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"
