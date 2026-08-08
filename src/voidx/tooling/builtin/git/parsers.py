"""Git output parsers — transform raw git stdout into structured data."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from voidx.tooling.domain.context import ToolExecutionContext as ToolContext
from voidx.platform.paths import resolve_tool_path as _resolve_tool_path
from voidx.tooling.application.authorization import sandbox_paths_for_access as _sandbox_paths_for_access
from voidx.observability.tool_log import log_tool_event

from voidx.tooling.builtin.git.constants import DIFF_HUNK_MAX_CHARS
from voidx.tooling.builtin.git.models import GitRepo
from voidx.tooling.builtin.git.process import run_git


def _display_path(repo_path: str, repo: GitRepo, workspace: str) -> str:
    if not repo_path:
        return ""
    resolved = (Path(repo.repo_root) / repo_path).resolve()
    try:
        return resolved.relative_to(Path(workspace).resolve()).as_posix()
    except ValueError:
        return Path(repo_path).as_posix()


def _pathspecs(paths: list[str], ctx: ToolContext, repo: GitRepo, *, allow_empty: bool) -> list[str]:
    if not paths:
        if allow_empty:
            return []
        raise ValueError("paths must not be empty")
    result = []
    repo_root = Path(repo.repo_root).resolve()
    for path in paths:
        resolved = _resolve_tool_path(ctx.workspace, path, _sandbox_paths_for_access(ctx, write=False))
        if resolved is None:
            raise ValueError(f"path outside allowed workspace: {path}")
        try:
            rel = resolved.resolve().relative_to(repo_root)
        except ValueError as exc:
            raise ValueError(f"path outside git repository: {path}") from exc
        result.append(rel.as_posix())
    return result


def _parse_show_numstat(numstat_raw: str, shortstat_raw: str, repo: GitRepo, workspace: str) -> tuple[list[str], dict[str, int]]:
    files_changed = []
    for line in numstat_raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        repo_path = parts[2]
        display_path = _display_path(repo_path, repo, workspace)
        files_changed.append(display_path)
    ins = re.search(r"(\d+) insertion", shortstat_raw)
    dels = re.search(r"(\d+) deletion", shortstat_raw)
    total_add = int(ins.group(1)) if ins else 0
    total_del = int(dels.group(1)) if dels else 0
    return files_changed, {"additions": total_add, "deletions": total_del}


def _parse_status(raw: str, repo: GitRepo, workspace: str) -> list[dict[str, Any]]:
    tokens = [token for token in raw.split("\0") if token]
    result = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if len(token) < 4:
            continue
        code = token[:2]
        repo_path = token[3:]
        original_path = ""
        if code[0] in {"R", "C"} and index < len(tokens):
            original_path = tokens[index]
            index += 1
        result.append({
            "path": _display_path(repo_path, repo, workspace),
            "staged": _status_label(code[0]),
            "unstaged": _status_label(code[1]),
            "untracked": code == "??",
            "original_path": _display_path(original_path, repo, workspace) if original_path else "",
        })
    return result


def _status_label(code: str) -> str:
    return {
        " ": "",
        "?": "",
        "!": "",
        "A": "added",
        "M": "modified",
        "D": "deleted",
        "R": "renamed",
        "C": "copied",
        "T": "typechange",
        "U": "unmerged",
    }.get(code, code)


def _diff_hunks(raw: str) -> tuple[list[str], bool]:
    truncated = len(raw) > DIFF_HUNK_MAX_CHARS
    if truncated:
        raw = raw[:DIFF_HUNK_MAX_CHARS].rstrip()
    hunks = []
    current = []
    for line in raw.splitlines():
        if line.startswith("@@ ") and current:
            hunks.append("\n".join(current))
            current = [line]
        elif line.startswith("@@ ") or current:
            current.append(line)
    if current:
        hunks.append("\n".join(current))
    return hunks, truncated


def _diff_hunks_by_path(raw: str, repo: GitRepo, workspace: str) -> dict[str, tuple[list[str], bool]]:
    result: dict[str, tuple[list[str], bool]] = {}
    current_path = ""
    current_lines: list[str] = []

    def flush() -> None:
        if current_path:
            result[current_path] = _diff_hunks("\n".join(current_lines))

    for line in raw.splitlines():
        if line.startswith("diff --git "):
            flush()
            repo_path = _parse_diff_git_path(line)
            current_path = _display_path(repo_path, repo, workspace) if repo_path else ""
            current_lines = []
        elif current_path:
            current_lines.append(line)
    flush()
    return result


def _parse_diff_git_path(line: str) -> str:
    marker = " b/"
    index = line.find(marker)
    if index == -1:
        return ""
    return line[index + len(marker):].strip().strip('"')


def _parse_log(raw: str, repo: GitRepo, workspace: str) -> list[dict[str, Any]]:
    entries = []
    current: dict[str, Any] | None = None
    for line in raw.splitlines():
        if "\x1f" in line:
            parts = line.split("\x1f")
            if len(parts) >= 4:
                if current is not None:
                    entries.append(current)
                current = {
                    "hash": parts[0],
                    "author": parts[1],
                    "date": parts[2],
                    "message": "\x1f".join(parts[3:]),
                    "files_changed": [],
                }
        elif line.strip() and current is not None:
            current["files_changed"].append(_display_path(line.strip(), repo, workspace))
    if current is not None:
        entries.append(current)
    return entries


def _parse_blame(raw: str) -> list[dict[str, Any]]:
    entries = []
    current: dict[str, Any] = {}
    for line in raw.splitlines():
        if not line:
            continue
        if line[0] == "\t":
            entries.append({
                "line": current.get("line", 0),
                "commit": current.get("commit", ""),
                "author": current.get("author", ""),
                "date": _format_blame_date(current),
                "content": line[1:],
            })
            current = {}
            continue
        parts = line.split(" ", 3)
        if len(parts) >= 3 and len(parts[0]) >= 7 and parts[1].isdigit():
            current["commit"] = parts[0]
            current["line"] = int(parts[2])
        elif line.startswith("author "):
            current["author"] = line.removeprefix("author ")
        elif line.startswith("author-time "):
            current["author_time"] = line.removeprefix("author-time ")
        elif line.startswith("author-tz "):
            current["author_tz"] = line.removeprefix("author-tz ")
    return entries


def _format_blame_date(current: dict[str, Any]) -> str:
    raw_time = str(current.get("author_time", ""))
    raw_tz = str(current.get("author_tz", ""))
    if not raw_time:
        return ""
    try:
        tz = _parse_git_timezone(raw_tz)
        return datetime.fromtimestamp(int(raw_time), tz=tz).isoformat()
    except ValueError:
        return raw_time


def _parse_git_timezone(raw: str) -> timezone:
    if len(raw) != 5 or raw[0] not in {"+", "-"}:
        return timezone.utc
    sign = 1 if raw[0] == "+" else -1
    hours = int(raw[1:3])
    minutes = int(raw[3:5])
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


async def _staged_files(ctx: ToolContext, repo: GitRepo) -> list[str]:
    proc = await run_git(repo, ["diff", "--cached", "--name-only"], read_only=True)
    if proc["returncode"] != 0:
        log_tool_event("git_staged_files_failed", tool_name="git", message=f"git diff --cached failed: {proc['stderr'].strip() or proc['stdout'].strip()}")
        return []
    return [_display_path(line, repo, ctx.workspace) for line in proc["stdout"].splitlines() if line.strip()]


async def _unstaged_files(ctx: ToolContext, repo: GitRepo) -> list[str]:
    proc = await run_git(repo, ["diff", "--name-only"], read_only=True)
    if proc["returncode"] != 0:
        log_tool_event("git_unstaged_files_failed", tool_name="git", message=f"git diff failed: {proc['stderr'].strip() or proc['stdout'].strip()}")
        return []
    return [_display_path(line, repo, ctx.workspace) for line in proc["stdout"].splitlines() if line.strip()]


async def _commit_files(ctx: ToolContext, repo: GitRepo, ref: str) -> list[str]:
    proc = await run_git(repo, ["diff-tree", "--no-commit-id", "--name-only", "-r", ref], read_only=True)
    if proc["returncode"] != 0:
        log_tool_event("git_commit_files_failed", tool_name="git", message=f"git diff-tree {ref} failed: {proc['stderr'].strip() or proc['stdout'].strip()}")
        return []
    return [_display_path(line, repo, ctx.workspace) for line in proc["stdout"].splitlines() if line.strip()]
