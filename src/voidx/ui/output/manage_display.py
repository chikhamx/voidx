"""Shared display helpers for manage tool actions."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from rich.cells import cell_len


def manage_display(args: dict[str, Any], *, limit: int = 72) -> tuple[str, str]:
    op = str(args.get("op") or "")
    if op == "create":
        return "Create", _shorten(_path_value(args), limit)
    if op == "delete":
        return "Remove", _shorten(_path_value(args), limit)
    if op == "move":
        action, value = _move_display(args)
        return action, _shorten(value, limit)
    return "Manage", _shorten(op, limit)


def _path_value(args: dict[str, Any]) -> str:
    paths = args.get("paths")
    if isinstance(paths, str) and paths:
        return paths
    if isinstance(paths, list):
        values = [str(path) for path in paths if path]
        if values:
            return _with_extra_count(values[0], len(values) - 1)
    for key in ("file_path", "path"):
        value = args.get(key)
        if value:
            return str(value)
    return ""


def _move_display(args: dict[str, Any]) -> tuple[str, str]:
    moves = args.get("moves")
    if isinstance(moves, list):
        values = [move for move in moves if isinstance(move, dict)]
        if values:
            src = str(values[0].get("src") or "")
            dest = str(values[0].get("dest") or "")
            action = "Rename" if _same_parent(src, dest) else "Move"
            value = _rename_value(src, dest) if action == "Rename" else _move_value(src, dest)
            return action, _with_extra_count(value, len(values) - 1)

    src = str(args.get("file_path") or args.get("path") or "")
    dest = str(args.get("dest_path") or args.get("dest") or "")
    action = "Rename" if _same_parent(src, dest) else "Move"
    value = _rename_value(src, dest) if action == "Rename" else _move_value(src, dest)
    return action, value


def _rename_value(src: str, dest: str) -> str:
    if src and dest:
        return f"{PurePosixPath(src).name} → {PurePosixPath(dest).name}"
    return src or dest


def _move_value(src: str, dest: str) -> str:
    if src and dest:
        return f"{src} → {dest}"
    return src or dest


def _same_parent(src: str, dest: str) -> bool:
    if not src or not dest:
        return False
    return PurePosixPath(src).parent == PurePosixPath(dest).parent


def _with_extra_count(value: str, extra_count: int) -> str:
    if extra_count <= 0:
        return value
    return f"{value} +{extra_count}"


def _shorten(text: str, limit: int) -> str:
    clean = " ".join(str(text).split())
    if limit <= 1:
        return clean[:limit]
    if cell_len(clean) <= limit:
        return clean
    if limit <= cell_len("…"):
        return "…"
    half = max((limit - cell_len("…")) // 2, 1)
    head = _take_cells(clean, half).rstrip()
    tail = _take_cells(clean, limit - cell_len(head) - cell_len("…"), from_end=True).lstrip()
    shortened = f"{head}…{tail}"
    while cell_len(shortened) > limit and tail:
        tail = tail[1:]
        shortened = f"{head}…{tail}"
    while cell_len(shortened) > limit and head:
        head = head[:-1].rstrip()
        shortened = f"{head}…{tail}"
    return shortened if cell_len(shortened) <= limit else "…"


def _take_cells(text: str, limit: int, *, from_end: bool = False) -> str:
    chars = reversed(text) if from_end else iter(text)
    picked: list[str] = []
    width = 0
    for char in chars:
        next_width = cell_len(char)
        if width + next_width > limit:
            break
        picked.append(char)
        width += next_width
    if from_end:
        picked.reverse()
    return "".join(picked)
