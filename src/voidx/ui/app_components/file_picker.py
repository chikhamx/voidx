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
MAX_SCAN_FILES = 5_000


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
    root = Path(workspace).resolve()
    if not root.exists() or not root.is_dir():
        return []
    normalized_query = query.strip().lower().replace("\\", "/")
    candidates: list[FileCandidate] = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS and not name.startswith(".")]
        rel_dir = Path(dirpath).resolve().relative_to(root).as_posix()
        for dirname in dirnames:
            path = Path(dirpath) / dirname
            try:
                rel_path = path.resolve().relative_to(root).as_posix()
            except ValueError:
                continue
            scanned += 1
            if scanned > MAX_SCAN_FILES:
                break
            rel_lower = rel_path.lower()
            if normalized_query and normalized_query not in rel_lower:
                continue
            try:
                item_count = sum(1 for _ in path.iterdir())
            except (OSError, PermissionError):
                item_count = 0
            candidates.append(FileCandidate(
                rel_path=rel_path + "/",
                kind="dir",
                size=item_count,
            ))
        for filename in filenames:
            if filename.startswith("."):
                continue
            path = Path(dirpath) / filename
            try:
                rel_path = path.resolve().relative_to(root).as_posix()
            except ValueError:
                continue
            scanned += 1
            if scanned > MAX_SCAN_FILES:
                break
            rel_lower = rel_path.lower()
            if normalized_query and normalized_query not in rel_lower:
                continue
            candidates.append(FileCandidate(
                rel_path=rel_path,
                kind="image" if is_image_file(rel_path) else "file",
                size=path.stat().st_size,
            ))
        if scanned > MAX_SCAN_FILES:
            break
    candidates.sort(key=lambda item: (
        not item.rel_path.lower().startswith(normalized_query),
        len(item.rel_path),
        item.rel_path,
    ))
    files = [c for c in candidates if c.kind != "dir"]
    dirs = [c for c in candidates if c.kind == "dir"]
    dir_slots = min(3, len(dirs))
    file_slots = limit - dir_slots
    result = files[:file_slots] + dirs[:dir_slots]
    if len(result) < limit:
        remaining = limit - len(result)
        result += files[file_slots:file_slots + remaining]
    return result


def attachment_token_text(rel_path: str) -> str:
    if any(ch.isspace() for ch in rel_path):
        return f'@"{rel_path}"'
    return f"@{rel_path}"


def is_image_file(path: str | Path) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXTENSIONS


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"
