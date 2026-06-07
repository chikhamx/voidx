"""Structured Git tool with path-scoped writes."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from voidx.tools.base import BaseTool, ToolContext, ToolResult, model_to_json_schema, resolve_safe


GIT_TIMEOUT_SECONDS = 15
DIFF_HUNK_MAX_CHARS = 12_000
LOG_LIMIT_MAX = 50
BLAME_RANGE_MAX = 200

GIT_READ_COMMANDS = {
    "status",
    "diff",
    "log",
    "blame",
    "branch_list",
    "remote_list",
}
GIT_WRITE_COMMANDS = {"add", "commit", "restore"}


class GitStatusArgs(BaseModel):
    pathspec: list[str] = Field(default_factory=list)


class GitDiffArgs(BaseModel):
    cached: bool = False
    pathspec: list[str] = Field(default_factory=list)
    ref: str = ""


class GitLogArgs(BaseModel):
    limit: int = Field(default=10, ge=1, le=LOG_LIMIT_MAX)
    path: str = ""
    author: str = ""
    since: str = ""


class GitBlameArgs(BaseModel):
    path: str = Field(min_length=1)
    start: int | None = Field(default=None, ge=1)
    end: int | None = Field(default=None, ge=1)


class GitBranchListArgs(BaseModel):
    all: bool = False


class GitAddArgs(BaseModel):
    paths: list[str] = Field(min_length=1)


class GitCommitArgs(BaseModel):
    message: str = Field(min_length=1)
    paths: list[str] = Field(default_factory=list)


class GitRestoreArgs(BaseModel):
    paths: list[str] = Field(min_length=1)
    staged: bool = False
    worktree: bool = True


class GitArgs(BaseModel):
    pathspec: list[str] = Field(default_factory=list)
    cached: bool = False
    ref: str = ""
    limit: int = 10
    path: str = ""
    author: str = ""
    since: str = ""
    start: int | None = None
    end: int | None = None
    all: bool = False
    paths: list[str] = Field(default_factory=list)
    message: str = ""
    staged: bool = False
    worktree: bool = True


class GitInput(BaseModel):
    command: Literal[
        "status",
        "diff",
        "log",
        "blame",
        "branch_list",
        "remote_list",
        "add",
        "commit",
        "restore",
    ] = Field(description="Git operation to run.")
    args: GitArgs = Field(default_factory=GitArgs, description="Command-specific arguments.")


class GitRepo(BaseModel):
    repo_root: str
    workspace: str


class GitTool(BaseTool):
    id = "git"
    description = (
        "Inspect and perform explicit path-scoped Git operations with structured JSON output. "
        "Read commands are status, diff, log, blame, branch_list, remote_list. "
        "Write commands are add, commit, restore and require approval."
    )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(GitInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = GitInput.model_validate(args)
        repo = await _discover_repo(ctx)
        if repo is None:
            return _result(inp.command, ctx, ok=False, error="not_a_git_repository")

        try:
            if inp.command == "status":
                return await _git_status(_args_dict(inp.args), ctx, repo)
            if inp.command == "diff":
                return await _git_diff(_args_dict(inp.args), ctx, repo)
            if inp.command == "log":
                return await _git_log(_args_dict(inp.args), ctx, repo)
            if inp.command == "blame":
                return await _git_blame(_args_dict(inp.args), ctx, repo)
            if inp.command == "branch_list":
                return await _git_branch_list(_args_dict(inp.args), ctx, repo)
            if inp.command == "remote_list":
                return await _git_remote_list(ctx, repo)
            if inp.command == "add":
                return await _git_add(_args_dict(inp.args), ctx, repo)
            if inp.command == "commit":
                return await _git_commit(_args_dict(inp.args), ctx, repo)
            if inp.command == "restore":
                return await _git_restore(_args_dict(inp.args), ctx, repo)
        except ValueError as exc:
            return _result(inp.command, ctx, repo=repo, ok=False, error=str(exc))

        return _result(inp.command, ctx, repo=repo, ok=False, error="unsupported_command")


def _args_dict(args: GitArgs) -> dict[str, Any]:
    return args.model_dump(mode="json")


async def _git_status(args: dict[str, Any], ctx: ToolContext, repo: GitRepo) -> ToolResult:
    inp = GitStatusArgs.model_validate(args)
    pathspec = _pathspecs(inp.pathspec, ctx, repo, allow_empty=True)
    proc = await _run_git(repo, ["status", "--porcelain=v1", "-z", "--", *pathspec], read_only=True)
    if proc["returncode"] != 0:
        return _result("status", ctx, repo=repo, ok=False, error=proc["stderr"] or proc["stdout"])
    entries = _parse_status(proc["stdout"], repo, ctx.workspace)
    return _result("status", ctx, repo=repo, data={"entries": entries})


async def _git_diff(args: dict[str, Any], ctx: ToolContext, repo: GitRepo) -> ToolResult:
    inp = GitDiffArgs.model_validate(args)
    pathspec = _pathspecs(inp.pathspec, ctx, repo, allow_empty=True)
    base = ["diff"]
    if inp.cached:
        base.append("--cached")
    if inp.ref:
        base.append(inp.ref)
    proc = await _run_git(repo, [*base, "--numstat", "--", *pathspec], read_only=True)
    if proc["returncode"] != 0:
        return _result("diff", ctx, repo=repo, ok=False, error=proc["stderr"] or proc["stdout"])
    hunk_proc = await _run_git(repo, [*base, "--unified=3", "--", *pathspec], read_only=True)
    if hunk_proc["returncode"] != 0:
        return _result("diff", ctx, repo=repo, ok=False, error=hunk_proc["stderr"] or hunk_proc["stdout"])
    hunks_by_path = _diff_hunks_by_path(hunk_proc["stdout"], repo, ctx.workspace)
    entries = []
    for line in proc["stdout"].splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added_raw, removed_raw, repo_path = parts[0], parts[1], parts[2]
        binary = added_raw == "-" or removed_raw == "-"
        display_path = _display_path(repo_path, repo, ctx.workspace)
        hunks, truncated = hunks_by_path.get(display_path, ([], False))
        entries.append({
            "path": display_path,
            "additions": 0 if binary else int(added_raw),
            "deletions": 0 if binary else int(removed_raw),
            "hunks": hunks,
            "binary": binary,
            "truncated": truncated,
        })
    return _result("diff", ctx, repo=repo, data={"entries": entries})


async def _git_log(args: dict[str, Any], ctx: ToolContext, repo: GitRepo) -> ToolResult:
    inp = GitLogArgs.model_validate(args)
    argv = [
        "log",
        f"-n{inp.limit}",
        "--name-only",
        "--pretty=format:%H%x1f%an%x1f%ad%x1f%s",
        "--date=iso-strict",
    ]
    if inp.author:
        argv.append(f"--author={inp.author}")
    if inp.since:
        argv.append(f"--since={inp.since}")
    if inp.path:
        argv.extend(["--", *_pathspecs([inp.path], ctx, repo, allow_empty=False)])
    proc = await _run_git(repo, argv, read_only=True)
    if proc["returncode"] != 0:
        return _result("log", ctx, repo=repo, ok=False, error=proc["stderr"] or proc["stdout"])
    return _result("log", ctx, repo=repo, data={"entries": _parse_log(proc["stdout"], repo, ctx.workspace)})


async def _git_blame(args: dict[str, Any], ctx: ToolContext, repo: GitRepo) -> ToolResult:
    inp = GitBlameArgs.model_validate(args)
    if inp.end is not None and inp.start is not None and inp.end < inp.start:
        raise ValueError("blame end must be greater than or equal to start")
    if inp.start is not None and inp.end is not None and inp.end - inp.start + 1 > BLAME_RANGE_MAX:
        raise ValueError(f"blame range must be at most {BLAME_RANGE_MAX} lines")
    repo_path = _pathspecs([inp.path], ctx, repo, allow_empty=False)[0]
    argv = ["blame", "--line-porcelain"]
    if inp.start is not None:
        end = inp.end or inp.start
        argv.extend([f"-L{inp.start},{end}"])
    argv.extend(["--", repo_path])
    proc = await _run_git(repo, argv, read_only=True)
    if proc["returncode"] != 0:
        return _result("blame", ctx, repo=repo, ok=False, error=proc["stderr"] or proc["stdout"])
    return _result("blame", ctx, repo=repo, data={"entries": _parse_blame(proc["stdout"])})


async def _git_branch_list(args: dict[str, Any], ctx: ToolContext, repo: GitRepo) -> ToolResult:
    inp = GitBranchListArgs.model_validate(args)
    argv = ["branch"]
    if inp.all:
        argv.append("--all")
    argv.extend(["--format=%(refname:short)\t%(HEAD)\t%(upstream:short)\t%(upstream:track)"])
    proc = await _run_git(repo, argv, read_only=True)
    if proc["returncode"] != 0:
        return _result("branch_list", ctx, repo=repo, ok=False, error=proc["stderr"] or proc["stdout"])
    entries = []
    for line in proc["stdout"].splitlines():
        if not line:
            continue
        name, current, upstream, track = [*line.split("\t"), "", "", "", ""][:4]
        ahead, behind = _parse_track(track)
        entries.append({
            "name": name,
            "current": current == "*",
            "upstream": upstream,
            "ahead": ahead,
            "behind": behind,
        })
    return _result("branch_list", ctx, repo=repo, data={"entries": entries})


async def _git_remote_list(ctx: ToolContext, repo: GitRepo) -> ToolResult:
    proc = await _run_git(repo, ["remote", "-v"], read_only=True)
    if proc["returncode"] != 0:
        return _result("remote_list", ctx, repo=repo, ok=False, error=proc["stderr"] or proc["stdout"])
    entries = []
    seen: set[tuple[str, str, str]] = set()
    for line in proc["stdout"].splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        kind = parts[2].strip("()")
        item = (parts[0], parts[1], kind)
        if item in seen:
            continue
        seen.add(item)
        entries.append({"name": parts[0], "url": parts[1], "type": kind})
    return _result("remote_list", ctx, repo=repo, data={"entries": entries})


async def _git_add(args: dict[str, Any], ctx: ToolContext, repo: GitRepo) -> ToolResult:
    inp = GitAddArgs.model_validate(args)
    pathspec = _pathspecs(inp.paths, ctx, repo, allow_empty=False)
    proc = await _run_git(repo, ["add", "--", *pathspec])
    if proc["returncode"] != 0:
        return _result("add", ctx, repo=repo, ok=False, error=proc["stderr"] or proc["stdout"])
    return _result("add", ctx, repo=repo, data={"staged": [_display_path(path, repo, ctx.workspace) for path in pathspec]})


async def _git_commit(args: dict[str, Any], ctx: ToolContext, repo: GitRepo) -> ToolResult:
    inp = GitCommitArgs.model_validate(args)
    message = inp.message.strip()
    if not message:
        raise ValueError("commit message must not be empty")
    if inp.paths:
        pathspec = _pathspecs(inp.paths, ctx, repo, allow_empty=False)
        add_proc = await _run_git(repo, ["add", "--", *pathspec])
        if add_proc["returncode"] != 0:
            return _result("commit", ctx, repo=repo, ok=False, error=add_proc["stderr"] or add_proc["stdout"])
        proc = await _run_git(repo, ["commit", "-m", message, "--only", "--", *pathspec])
        if proc["returncode"] != 0:
            return _result("commit", ctx, repo=repo, ok=False, error=proc["stderr"] or proc["stdout"], data={
                "requested_paths": [_display_path(path, repo, ctx.workspace) for path in pathspec],
                "unstaged_uncommitted": await _unstaged_files(ctx, repo),
            })
    if not inp.paths:
        staged = await _staged_files(ctx, repo)
        if not staged:
            return _result("commit", ctx, repo=repo, ok=False, error="nothing_staged")
        proc = await _run_git(repo, ["commit", "-m", message])
        if proc["returncode"] != 0:
            return _result("commit", ctx, repo=repo, ok=False, error=proc["stderr"] or proc["stdout"], data={
                "staged": staged,
                "unstaged_uncommitted": await _unstaged_files(ctx, repo),
            })
    rev = await _run_git(repo, ["rev-parse", "HEAD"], read_only=True)
    commit_hash = rev["stdout"].strip() if rev["returncode"] == 0 else ""
    files_changed = await _commit_files(ctx, repo, "HEAD")
    return _result("commit", ctx, repo=repo, data={
        "hash": commit_hash,
        "message": message,
        "files_changed": files_changed,
        "unstaged_uncommitted": await _unstaged_files(ctx, repo),
    })


async def _git_restore(args: dict[str, Any], ctx: ToolContext, repo: GitRepo) -> ToolResult:
    inp = GitRestoreArgs.model_validate(args)
    if not inp.staged and not inp.worktree:
        raise ValueError("restore must target staged and/or worktree")
    pathspec = _pathspecs(inp.paths, ctx, repo, allow_empty=False)
    argv = ["restore"]
    if inp.staged:
        argv.append("--staged")
    if inp.worktree:
        argv.append("--worktree")
    argv.extend(["--", *pathspec])
    proc = await _run_git(repo, argv)
    if proc["returncode"] != 0:
        return _result("restore", ctx, repo=repo, ok=False, error=proc["stderr"] or proc["stdout"])
    return _result("restore", ctx, repo=repo, data={
        "restored": [_display_path(path, repo, ctx.workspace) for path in pathspec],
        "warning": "restore may overwrite worktree files; use /rollback for current-turn agent edits",
    })


async def _discover_repo(ctx: ToolContext) -> GitRepo | None:
    proc = await _run_process(["git", "rev-parse", "--show-toplevel"], cwd=ctx.workspace, read_only=True)
    if proc["returncode"] != 0:
        return None
    return GitRepo(
        repo_root=str(Path(proc["stdout"].strip()).resolve()),
        workspace=str(Path(ctx.workspace).resolve()),
    )


async def _run_git(repo: GitRepo, args: list[str], *, read_only: bool = False) -> dict[str, Any]:
    return await _run_process(["git", *args], cwd=repo.repo_root, read_only=read_only)


async def _run_process(args: list[str], *, cwd: str, read_only: bool = False) -> dict[str, Any]:
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
    }
    if read_only:
        env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=GIT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"returncode": -1, "stdout": "", "stderr": f"git command timed out after {GIT_TIMEOUT_SECONDS}s"}
    except FileNotFoundError:
        return {"returncode": -1, "stdout": "", "stderr": "git executable not found"}
    return {
        "returncode": proc.returncode or 0,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
    }


def _pathspecs(paths: list[str], ctx: ToolContext, repo: GitRepo, *, allow_empty: bool) -> list[str]:
    if not paths:
        if allow_empty:
            return []
        raise ValueError("paths must not be empty")
    result = []
    repo_root = Path(repo.repo_root).resolve()
    for path in paths:
        resolved = resolve_safe(ctx.workspace, path, ctx.sandbox_extra_paths)
        if resolved is None:
            raise ValueError(f"path outside allowed workspace: {path}")
        try:
            rel = resolved.resolve().relative_to(repo_root)
        except ValueError as exc:
            raise ValueError(f"path outside git repository: {path}") from exc
        result.append(rel.as_posix())
    return result


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
    proc = await _run_git(repo, ["diff", "--cached", "--name-only"], read_only=True)
    if proc["returncode"] != 0:
        return []
    return [_display_path(line, repo, ctx.workspace) for line in proc["stdout"].splitlines() if line.strip()]


async def _unstaged_files(ctx: ToolContext, repo: GitRepo) -> list[str]:
    proc = await _run_git(repo, ["diff", "--name-only"], read_only=True)
    if proc["returncode"] != 0:
        return []
    return [_display_path(line, repo, ctx.workspace) for line in proc["stdout"].splitlines() if line.strip()]


async def _commit_files(ctx: ToolContext, repo: GitRepo, ref: str) -> list[str]:
    proc = await _run_git(repo, ["diff-tree", "--no-commit-id", "--name-only", "-r", ref], read_only=True)
    if proc["returncode"] != 0:
        return []
    return [_display_path(line, repo, ctx.workspace) for line in proc["stdout"].splitlines() if line.strip()]


def _display_path(repo_path: str, repo: GitRepo, workspace: str) -> str:
    if not repo_path:
        return ""
    resolved = (Path(repo.repo_root) / repo_path).resolve()
    try:
        return resolved.relative_to(Path(workspace).resolve()).as_posix()
    except ValueError:
        return Path(repo_path).as_posix()


def _parse_track(track: str) -> tuple[int, int]:
    ahead = 0
    behind = 0
    clean = track.strip("[] ")
    for part in clean.split(","):
        part = part.strip()
        if part.startswith("ahead "):
            ahead = int(part.removeprefix("ahead "))
        elif part.startswith("behind "):
            behind = int(part.removeprefix("behind "))
    return ahead, behind


def _result(
    command: str,
    ctx: ToolContext,
    *,
    repo: GitRepo | None = None,
    ok: bool = True,
    data: dict[str, Any] | None = None,
    error: str = "",
) -> ToolResult:
    payload = {
        "ok": ok,
        "command": command,
        "repo_root": repo.repo_root if repo else "",
        "workspace": repo.workspace if repo else str(Path(ctx.workspace).resolve()),
        "data": data or {},
        "error": error.strip(),
    }
    return ToolResult(
        title=f"git: {command}",
        output=json.dumps(payload, ensure_ascii=False, indent=2),
        metadata={
            "command": command,
            "ok": ok,
            "error": not ok,
            "error_message": error.strip(),
        },
    )
