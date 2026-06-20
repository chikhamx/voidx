"""Structured Git tool with path-scoped writes."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from voidx.tools.base import BaseTool, ToolContext, ToolResult, model_to_json_schema, resolve_safe


GIT_TIMEOUT_SECONDS = 15
GIT_REMOTE_TIMEOUT_SECONDS = 60
DIFF_HUNK_MAX_CHARS = 12_000
HOOK_OUTPUT_MAX_CHARS = 4000
LOG_LIMIT_MAX = 50
BLAME_RANGE_MAX = 200
_BRANCH_NAME_RE = re.compile(r"^(?!\.)(?!-)[a-zA-Z0-9/_-]+(\.[a-zA-Z0-9/_-]+)*$")
_BRANCH_NAME_DENY = re.compile(r"\.\.|[@~^:\\\s]|\.lock$")
_SAFE_REMOTE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/-]*$")
_SAFE_REF_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/@+-]*$")

GIT_READ_COMMANDS = {
    "status",
    "diff",
    "log",
    "blame",
    "branch_list",
    "remote_list",
    "show",
    "tag_list",
}
GIT_WRITE_COMMANDS = {
    "add",
    "commit",
    "restore",
    "switch",
    "branch_create",
    "branch_delete",
    "tag_create",
    "tag_delete",
    "stash_push",
    "stash_pop",
    "push",
    "pull",
    "fetch",
    "merge",
    "rebase",
}


class GitStatusArgs(BaseModel):
    pathspec: list[str] = Field(default_factory=list)


class GitDiffArgs(BaseModel):
    cached: bool = False
    pathspec: list[str] = Field(default_factory=list)
    ref: str = ""
    base: str = ""


class GitLogArgs(BaseModel):
    limit: int = Field(default=10, ge=1, le=LOG_LIMIT_MAX)
    path: str = ""
    author: str = ""
    since: str = ""
    until: str = ""


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



class GitSwitchArgs(BaseModel):
    branch: str = Field(min_length=1)
    create: bool = False
    start_point: str = ""


class GitShowArgs(BaseModel):
    ref: str = "HEAD"
    stat: bool = False
    pathspec: list[str] = Field(default_factory=list)


class GitBranchCreateArgs(BaseModel):
    name: str = Field(min_length=1)
    start_point: str = ""


class GitBranchDeleteArgs(BaseModel):
    name: str = Field(min_length=1)
    force: bool = False


class GitTagListArgs(BaseModel):
    pattern: str = ""
    sort: str = ""


class GitTagCreateArgs(BaseModel):
    name: str = Field(min_length=1)
    ref: str = ""
    message: str = ""
    force: bool = False


class GitTagDeleteArgs(BaseModel):
    name: str = Field(min_length=1)


class GitStashPushArgs(BaseModel):
    message: str = ""
    pathspec: list[str] = Field(default_factory=list)


class GitStashPopArgs(BaseModel):
    index: int = Field(default=0, ge=0)
    keep: bool = False



class GitPushArgs(BaseModel):
    remote: str = "origin"
    branch: str = ""
    force: bool = False
    all_branches: bool = False

    @model_validator(mode="after")
    def _validate_push_args(self):
        if self.all_branches and self.branch:
            raise ValueError("all_branches and branch are mutually exclusive")
        return self


class GitPullArgs(BaseModel):
    remote: str = "origin"
    branch: str = ""


class GitFetchArgs(BaseModel):
    remote: str = "origin"
    branch: str = ""
    all: bool = False
    prune: bool = False

    @model_validator(mode="after")
    def _validate_fetch_args(self):
        if self.all and self.branch:
            raise ValueError("all and branch are mutually exclusive")
        return self


class GitMergeArgs(BaseModel):
    branch: str = Field(min_length=1)
    message: str = ""
    no_ff: bool = False


class GitRebaseArgs(BaseModel):
    branch: str = ""
    onto: str = ""
    continue_rebase: bool = False
    abort: bool = False

    @model_validator(mode="after")
    def _validate_rebase_args(self):
        if self.continue_rebase and self.abort:
            raise ValueError("continue_rebase and abort are mutually exclusive")
        if not self.continue_rebase and not self.abort and not self.branch:
            raise ValueError("branch is required when not continuing or aborting a rebase")
        return self

class GitArgs(BaseModel):
    pathspec: list[str] = Field(default_factory=list)
    cached: bool = False
    ref: str = ""
    limit: int = 10
    path: str = ""
    author: str = ""
    since: str = ""
    until: str = ""
    start: int | None = None
    end: int | None = None
    all: bool = False
    paths: list[str] = Field(default_factory=list)
    message: str = ""
    staged: bool = False
    worktree: bool = True
    branch: str = ""
    create: bool = False
    start_point: str = ""
    stat: bool = False
    base: str = ""
    name: str = ""
    force: bool = False
    pattern: str = ""
    sort: str = ""
    index: int = 0
    keep: bool = False
    remote: str = "origin"
    all_branches: bool = False
    prune: bool = False
    no_ff: bool = False
    onto: str = ""
    continue_rebase: bool = False
    abort: bool = False


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
        "show",
        "switch",
        "branch_create",
        "branch_delete",
        "tag_list",
        "tag_create",
        "tag_delete",
        "stash_push",
        "stash_pop",
        "push",
        "pull",
        "fetch",
        "merge",
        "rebase",
    ] = Field(description="Git operation to run.")
    args: GitArgs = Field(default_factory=GitArgs, description="Command-specific arguments.")


class GitRepo(BaseModel):
    repo_root: str
    workspace: str


class GitTool(BaseTool):
    id = "git"
    description = (
        "Inspect and perform explicit path-scoped Git operations with structured JSON output. "
        "Read commands are status, diff, log, blame, branch_list, remote_list, show, tag_list. "
        "Write commands are add, commit, restore, switch, branch_create, branch_delete, "
        "tag_create, tag_delete, stash_push, stash_pop, push, pull, fetch, merge, rebase "
        "and require approval."
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
            if inp.command == "show":
                return await _git_show(_args_dict(inp.args), ctx, repo)
            if inp.command == "switch":
                return await _git_switch(_args_dict(inp.args), ctx, repo)
            if inp.command == "branch_create":
                return await _git_branch_create(_args_dict(inp.args), ctx, repo)
            if inp.command == "branch_delete":
                return await _git_branch_delete(_args_dict(inp.args), ctx, repo)
            if inp.command == "tag_list":
                return await _git_tag_list(_args_dict(inp.args), ctx, repo)
            if inp.command == "tag_create":
                return await _git_tag_create(_args_dict(inp.args), ctx, repo)
            if inp.command == "tag_delete":
                return await _git_tag_delete(_args_dict(inp.args), ctx, repo)
            if inp.command == "stash_push":
                return await _git_stash_push(_args_dict(inp.args), ctx, repo)
            if inp.command == "stash_pop":
                return await _git_stash_pop(_args_dict(inp.args), ctx, repo)
            if inp.command == "push":
                return await _git_push(_args_dict(inp.args), ctx, repo)
            if inp.command == "pull":
                return await _git_pull(_args_dict(inp.args), ctx, repo)
            if inp.command == "fetch":
                return await _git_fetch(_args_dict(inp.args), ctx, repo)
            if inp.command == "merge":
                return await _git_merge(_args_dict(inp.args), ctx, repo)
            if inp.command == "rebase":
                return await _git_rebase(_args_dict(inp.args), ctx, repo)
        except ValueError as exc:
            from pydantic import ValidationError as _VE
            if isinstance(exc, _VE):
                fields = [". ".join(str(p) for p in e.get("loc", ())) for e in exc.errors() if e.get("loc")]
                if fields:
                    return _result(inp.command, ctx, repo=repo, ok=False,
                                   error=f"Invalid argument: {', '.join(fields)}. Check the parameter schema and retry.")
                detail = "; ".join(e.get("msg", str(e)) for e in exc.errors())
                return _result(inp.command, ctx, repo=repo, ok=False,
                               error=f"Invalid argument: {detail}. Check the parameter schema and retry.")
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
    branch_proc = await _run_git(repo, ["symbolic-ref", "--short", "HEAD"], read_only=True)
    branch = branch_proc["stdout"].strip() if branch_proc["returncode"] == 0 else ""
    return _result("status", ctx, repo=repo, data={"entries": entries, "branch": branch})


async def _git_diff(args: dict[str, Any], ctx: ToolContext, repo: GitRepo) -> ToolResult:
    inp = GitDiffArgs.model_validate(args)
    pathspec = _pathspecs(inp.pathspec, ctx, repo, allow_empty=True)
    base_argv = ["diff"]
    if inp.cached:
        base_argv.append("--cached")
    if inp.base and inp.ref:
        base_argv.extend([inp.base, inp.ref])
    elif inp.ref:
        base_argv.append(inp.ref)
    proc = await _run_git(repo, [*base_argv, "--numstat", "--", *pathspec], read_only=True)
    if proc["returncode"] != 0:
        return _result("diff", ctx, repo=repo, ok=False, error=proc["stderr"] or proc["stdout"])
    hunk_proc = await _run_git(repo, [*base_argv, "--unified=3", "--", *pathspec], read_only=True)
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
    if inp.until:
        argv.append(f"--until={inp.until}")
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
    hook_output = ""
    if proc.get("stderr") or proc.get("stdout"):
        parts = []
        if proc.get("stdout"):
            parts.append(proc["stdout"].strip())
        if proc.get("stderr"):
            parts.append(proc["stderr"].strip())
        hook_output = "\n---\n".join(parts)
        if len(hook_output) > HOOK_OUTPUT_MAX_CHARS:
            hook_output = hook_output[:HOOK_OUTPUT_MAX_CHARS] + "[truncated]"
    return _result("commit", ctx, repo=repo, data={
        "hash": commit_hash,
        "message": message,
        "files_changed": files_changed,
        "unstaged_uncommitted": await _unstaged_files(ctx, repo),
        "hook_output": hook_output,
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



async def _git_show(args: dict[str, Any], ctx: ToolContext, repo: GitRepo) -> ToolResult:
    inp = GitShowArgs.model_validate(args)
    pathspec = _pathspecs(inp.pathspec, ctx, repo, allow_empty=True)
    ref = inp.ref or "HEAD"
    meta_argv = [
        "show", f"--format=%H%x1f%an%x1f%ad%x1f%s%x1f%P", "--no-patch", ref,
    ]
    meta_proc = await _run_git(repo, meta_argv, read_only=True)
    if meta_proc["returncode"] != 0:
        return _result("show", ctx, repo=repo, ok=False, error="ref_not_found")
    meta_line = meta_proc["stdout"].strip()
    parts = meta_line.split("\x1f")
    if len(parts) < 4:
        return _result("show", ctx, repo=repo, ok=False, error="failed to parse commit metadata")
    commit_hash, author, date, message = parts[0], parts[1], parts[2], parts[3]
    parents = parts[4].split() if len(parts) > 4 and parts[4] else []
    is_merge = len(parents) > 1
    if inp.stat:
        numstat_argv = ["show", "--format=", "--numstat", ref, "--", *pathspec]
        numstat_proc = await _run_git(repo, numstat_argv, read_only=True)
        if numstat_proc["returncode"] != 0:
            return _result("show", ctx, repo=repo, ok=False, error=numstat_proc["stderr"] or numstat_proc["stdout"])
        shortstat_argv = ["show", "--format=", "--shortstat", ref, "--", *pathspec]
        shortstat_proc = await _run_git(repo, shortstat_argv, read_only=True)
        if shortstat_proc["returncode"] != 0:
            return _result("show", ctx, repo=repo, ok=False, error=shortstat_proc["stderr"] or shortstat_proc["stdout"])
        files_changed, stats = _parse_show_numstat(numstat_proc["stdout"], shortstat_proc["stdout"], repo, ctx.workspace)
        return _result("show", ctx, repo=repo, data={
            "hash": commit_hash, "author": author, "date": date, "message": message,
            "parents": parents, "merge": is_merge,
            "files_changed": files_changed, "stats": stats,
            "hunks": [], "truncated": False,
        })
    diff_argv = ["show", "--format=", "--unified=3", ref, "--", *pathspec]
    diff_proc = await _run_git(repo, diff_argv, read_only=True)
    if diff_proc["returncode"] != 0:
        return _result("show", ctx, repo=repo, ok=False, error=diff_proc["stderr"] or diff_proc["stdout"])
    numstat_argv = ["show", "--format=", "--numstat", ref, "--", *pathspec]
    numstat_proc = await _run_git(repo, numstat_argv, read_only=True)
    if numstat_proc["returncode"] != 0:
        return _result("show", ctx, repo=repo, ok=False, error=numstat_proc["stderr"] or numstat_proc["stdout"])
    hunks_by_path = _diff_hunks_by_path(diff_proc["stdout"], repo, ctx.workspace)
    files_changed = []
    total_add = 0
    total_del = 0
    for line in numstat_proc["stdout"].splitlines():
        p = line.split("\t")
        if len(p) < 3:
            continue
        added_raw, removed_raw, repo_path = p[0], p[1], p[2]
        binary = added_raw == "-" or removed_raw == "-"
        display_path = _display_path(repo_path, repo, ctx.workspace)
        if not binary:
            total_add += int(added_raw)
            total_del += int(removed_raw)
        files_changed.append(display_path)
    hunks = []
    truncated = False
    if not is_merge:
        for path in files_changed:
            path_hunks, path_trunc = hunks_by_path.get(path, ([], False))
            hunks.extend(path_hunks)
            truncated = truncated or path_trunc
    return _result("show", ctx, repo=repo, data={
        "hash": commit_hash, "author": author, "date": date, "message": message,
        "parents": parents, "merge": is_merge,
        "files_changed": files_changed,
        "stats": {"additions": total_add, "deletions": total_del},
        "hunks": hunks, "truncated": truncated,
    })


async def _git_switch(args: dict[str, Any], ctx: ToolContext, repo: GitRepo) -> ToolResult:
    inp = GitSwitchArgs.model_validate(args)
    if not _BRANCH_NAME_RE.match(inp.branch) or _BRANCH_NAME_DENY.search(inp.branch):
        raise ValueError(f"invalid branch name: {inp.branch}")
    prev_proc = await _run_git(repo, ["symbolic-ref", "--short", "HEAD"], read_only=True)
    previous_branch = prev_proc["stdout"].strip() if prev_proc["returncode"] == 0 else ""
    if inp.create:
        argv = ["switch", "-c", inp.branch]
        if inp.start_point:
            argv.append(inp.start_point)
    else:
        status_proc = await _run_git(repo, ["status", "--porcelain"], read_only=True)
        if status_proc["returncode"] == 0 and status_proc["stdout"].strip():
            dirty_files = [
                _display_path(line[3:].strip(), repo, ctx.workspace)
                for line in status_proc["stdout"].splitlines()
                if line.strip()
            ]
            proc = await _run_git(repo, ["switch", inp.branch])
            if proc["returncode"] != 0:
                return _result("switch", ctx, repo=repo, ok=False, error="dirty_conflict",
                               data={"dirty_files": dirty_files,
                                     "suggestion": "stash_push before switching branches"})
        else:
            proc = await _run_git(repo, ["switch", inp.branch])
            if proc["returncode"] != 0:
                stderr = proc["stderr"] or proc["stdout"]
                if "did not match" in stderr or "not found" in stderr.lower() or "invalid reference" in stderr.lower():
                    return _result("switch", ctx, repo=repo, ok=False, error="branch_not_found")
                return _result("switch", ctx, repo=repo, ok=False, error=stderr)
        return _result("switch", ctx, repo=repo, data={
            "branch": inp.branch, "created": False, "previous_branch": previous_branch,
        })
    proc = await _run_git(repo, argv)
    if proc["returncode"] != 0:
        return _result("switch", ctx, repo=repo, ok=False, error=proc["stderr"] or proc["stdout"])
    return _result("switch", ctx, repo=repo, data={
        "branch": inp.branch, "created": True, "previous_branch": previous_branch,
    })


async def _git_branch_create(args: dict[str, Any], ctx: ToolContext, repo: GitRepo) -> ToolResult:
    inp = GitBranchCreateArgs.model_validate(args)
    if not _BRANCH_NAME_RE.match(inp.name) or _BRANCH_NAME_DENY.search(inp.name):
        raise ValueError(f"invalid branch name: {inp.name}")
    argv = ["branch", inp.name]
    if inp.start_point:
        argv.append(inp.start_point)
    proc = await _run_git(repo, argv)
    if proc["returncode"] != 0:
        return _result("branch_create", ctx, repo=repo, ok=False, error=proc["stderr"] or proc["stdout"])
    rev_proc = await _run_git(repo, ["rev-parse", "--short", inp.name], read_only=True)
    branch_hash = rev_proc["stdout"].strip() if rev_proc["returncode"] == 0 else ""
    return _result("branch_create", ctx, repo=repo, data={
        "name": inp.name, "start_point": inp.start_point or "HEAD", "hash": branch_hash,
    })


async def _git_branch_delete(args: dict[str, Any], ctx: ToolContext, repo: GitRepo) -> ToolResult:
    inp = GitBranchDeleteArgs.model_validate(args)
    if not _BRANCH_NAME_RE.match(inp.name) or _BRANCH_NAME_DENY.search(inp.name):
        raise ValueError(f"invalid branch name: {inp.name}")
    argv = ["branch"]
    if inp.force:
        argv.append("-D")
    else:
        argv.append("-d")
    argv.append(inp.name)
    proc = await _run_git(repo, argv)
    if proc["returncode"] != 0:
        stderr = proc["stderr"] or proc["stdout"]
        if "not found" in stderr.lower() or "did not match" in stderr:
            return _result("branch_delete", ctx, repo=repo, ok=False, error="branch_not_found")
        if "cannot delete" in stderr.lower():
            return _result("branch_delete", ctx, repo=repo, ok=False, error="cannot_delete_current_branch")
        if "not fully merged" in stderr.lower() or "unmerged" in stderr.lower():
            return _result("branch_delete", ctx, repo=repo, ok=False, error="branch_not_merged")
        return _result("branch_delete", ctx, repo=repo, ok=False, error=stderr)
    return _result("branch_delete", ctx, repo=repo, data={"name": inp.name, "force": inp.force})


async def _git_tag_list(args: dict[str, Any], ctx: ToolContext, repo: GitRepo) -> ToolResult:
    inp = GitTagListArgs.model_validate(args)
    argv = ["tag", "-l", "--format=%(refname:short) %(objectname:short)"]
    if inp.pattern:
        argv.append(inp.pattern)
    if inp.sort:
        argv.append(f"--sort={inp.sort}")
    proc = await _run_git(repo, argv, read_only=True)
    if proc["returncode"] != 0:
        return _result("tag_list", ctx, repo=repo, ok=False, error=proc["stderr"] or proc["stdout"])
    entries = []
    for line in proc["stdout"].splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        tag_name = parts[0]
        tag_hash = parts[1] if len(parts) > 1 else ""
        entries.append({"name": tag_name, "hash": tag_hash})
    return _result("tag_list", ctx, repo=repo, data={"entries": entries})


async def _git_tag_create(args: dict[str, Any], ctx: ToolContext, repo: GitRepo) -> ToolResult:
    inp = GitTagCreateArgs.model_validate(args)
    argv = ["tag"]
    if inp.message:
        argv.extend(["-a", "-m", inp.message])
    if inp.force:
        argv.append("-f")
    argv.append(inp.name)
    if inp.ref:
        argv.append(inp.ref)
    proc = await _run_git(repo, argv)
    if proc["returncode"] != 0:
        stderr = proc["stderr"] or proc["stdout"]
        if "already exists" in stderr:
            return _result("tag_create", ctx, repo=repo, ok=False, error="tag_already_exists")
        return _result("tag_create", ctx, repo=repo, ok=False, error=stderr)
    rev_proc = await _run_git(repo, ["rev-list", "-1", inp.name], read_only=True)
    tag_hash = rev_proc["stdout"].strip()[:7] if rev_proc["returncode"] == 0 else ""
    return _result("tag_create", ctx, repo=repo, data={
        "name": inp.name, "ref": inp.ref or "HEAD", "hash": tag_hash,
        "annotated": bool(inp.message),
    })


async def _git_tag_delete(args: dict[str, Any], ctx: ToolContext, repo: GitRepo) -> ToolResult:
    inp = GitTagDeleteArgs.model_validate(args)
    proc = await _run_git(repo, ["tag", "-d", inp.name])
    if proc["returncode"] != 0:
        return _result("tag_delete", ctx, repo=repo, ok=False, error=proc["stderr"] or proc["stdout"])
    return _result("tag_delete", ctx, repo=repo, data={"name": inp.name})


async def _git_stash_push(args: dict[str, Any], ctx: ToolContext, repo: GitRepo) -> ToolResult:
    inp = GitStashPushArgs.model_validate(args)
    argv = ["stash", "push"]
    if inp.message:
        argv.extend(["-m", inp.message])
    if inp.pathspec:
        pathspec = _pathspecs(inp.pathspec, ctx, repo, allow_empty=False)
        argv.append("--")
        argv.extend(pathspec)
    proc = await _run_git(repo, argv)
    if proc["returncode"] != 0:
        return _result("stash_push", ctx, repo=repo, ok=False, error=proc["stderr"] or proc["stdout"])
    stash_msg = proc["stdout"].strip()
    files_stashed = []
    show_proc = await _run_git(repo, ["stash", "show", "--name-only", "stash@{0}"], read_only=True)
    if show_proc["returncode"] == 0:
        files_stashed = [_display_path(l, repo, ctx.workspace) for l in show_proc["stdout"].splitlines() if l.strip()]
    return _result("stash_push", ctx, repo=repo, data={
        "index": 0, "message": stash_msg, "files_stashed": files_stashed,
    })


async def _git_stash_pop(args: dict[str, Any], ctx: ToolContext, repo: GitRepo) -> ToolResult:
    inp = GitStashPopArgs.model_validate(args)
    subcmd = "apply" if inp.keep else "pop"
    stash_ref = f"stash@{{{inp.index}}}"
    show_proc = await _run_git(repo, ["stash", "show", "--name-only", stash_ref], read_only=True)
    stash_files = []
    if show_proc["returncode"] == 0:
        stash_files = [_display_path(l, repo, ctx.workspace) for l in show_proc["stdout"].splitlines() if l.strip()]
    argv = ["stash", subcmd, stash_ref]
    proc = await _run_git(repo, argv)
    if proc["returncode"] != 0:
        stderr = proc["stderr"] or proc["stdout"]
        conflicts = []
        if "CONFLICT" in proc["stdout"] or "CONFLICT" in stderr:
            for line in (proc["stdout"] + stderr).splitlines():
                if line.startswith("CONFLICT"):
                    parts = line.split()
                    if len(parts) >= 3:
                        conflicts.append(parts[-1])
        return _result("stash_pop", ctx, repo=repo, ok=False, error=stderr,
                       data={"index": inp.index, "applied": False, "kept": inp.keep,
                             "conflicts": conflicts, "files_restored": stash_files})
    return _result("stash_pop", ctx, repo=repo, data={
        "index": inp.index, "applied": True, "kept": inp.keep,
        "conflicts": [], "files_restored": stash_files,
    })



async def _git_push(args: dict[str, Any], ctx: ToolContext, repo: GitRepo) -> ToolResult:
    inp = GitPushArgs.model_validate(args)
    if not _SAFE_REMOTE_RE.match(inp.remote):
        raise ValueError(f"invalid remote name: {inp.remote}")
    remote_check = await _run_git(repo, ["remote", "get-url", inp.remote], read_only=True)
    if remote_check["returncode"] != 0:
        return _result("push", ctx, repo=repo, ok=False, error="remote_not_found")

    _PROTECTED_BRANCHES = {"main", "master"}

    if inp.force:
        if inp.all_branches:
            return _result("push", ctx, repo=repo, ok=False,
                           error="force push with --all is blocked: would force-push protected branches",
                           data={"remote": inp.remote, "force": True, "all_branches": True})
        target_branch = inp.branch
        if not target_branch:
            head_proc = await _run_git(repo, ["symbolic-ref", "--short", "HEAD"], read_only=True)
            if head_proc["returncode"] == 0:
                target_branch = head_proc["stdout"].strip()
        if target_branch and target_branch in _PROTECTED_BRANCHES:
            return _result("push", ctx, repo=repo, ok=False,
                           error=f"force push to protected branch '{target_branch}' is blocked",
                           data={"remote": inp.remote, "branch": target_branch, "force": True})

    argv = ["push"]
    if inp.force:
        argv.append("--force")
    if inp.all_branches:
        argv.append("--all")
    argv.append(inp.remote)
    if inp.branch:
        if not _BRANCH_NAME_RE.match(inp.branch) or _BRANCH_NAME_DENY.search(inp.branch):
            raise ValueError(f"invalid branch name: {inp.branch}")
        argv.append(inp.branch)
    proc = await _run_git(repo, argv, timeout=GIT_REMOTE_TIMEOUT_SECONDS)
    if proc["returncode"] != 0:
        stderr = proc["stderr"] or proc["stdout"]
        if "[rejected]" in stderr or "non-fast-forward" in stderr.lower() or "fetch first" in stderr.lower():
            return _result("push", ctx, repo=repo, ok=False, error="push_rejected",
                           data={"remote": inp.remote, "branch": inp.branch, "force": inp.force,
                                 "suggestion": "use force=True to overwrite remote history"})
        return _result("push", ctx, repo=repo, ok=False, error=stderr)
    summary = proc["stdout"].strip() or proc["stderr"].strip() or "pushed"
    return _result("push", ctx, repo=repo, data={
        "remote": inp.remote, "branch": inp.branch, "force": inp.force,
        "summary": summary.split("\n")[-1] if "\n" in summary else summary,
    })


async def _git_pull(args: dict[str, Any], ctx: ToolContext, repo: GitRepo) -> ToolResult:
    inp = GitPullArgs.model_validate(args)
    if not _SAFE_REMOTE_RE.match(inp.remote):
        raise ValueError(f"invalid remote name: {inp.remote}")
    remote_check = await _run_git(repo, ["remote", "get-url", inp.remote], read_only=True)
    if remote_check["returncode"] != 0:
        return _result("pull", ctx, repo=repo, ok=False, error="remote_not_found")
    argv = ["pull"]
    argv.append(inp.remote)
    if inp.branch:
        if not _BRANCH_NAME_RE.match(inp.branch) or _BRANCH_NAME_DENY.search(inp.branch):
            raise ValueError(f"invalid branch name: {inp.branch}")
        argv.append(inp.branch)
    proc = await _run_git(repo, argv, timeout=GIT_REMOTE_TIMEOUT_SECONDS)
    if proc["returncode"] != 0:
        stderr = proc["stderr"] or proc["stdout"]
        combined = proc["stdout"] + stderr
        if "CONFLICT" in combined:
            conflicts = _parse_conflicts(combined)
            return _result("pull", ctx, repo=repo, ok=False, error="merge_conflict",
                           data={"remote": inp.remote, "branch": inp.branch, "conflicts": conflicts})
        return _result("pull", ctx, repo=repo, ok=False, error=stderr)
    fast_forward = "Fast-forward" in proc["stdout"]
    summary = proc["stdout"].strip() or "Already up to date."
    return _result("pull", ctx, repo=repo, data={
        "remote": inp.remote, "branch": inp.branch,
        "fast_forward": fast_forward, "summary": summary.split("\n")[-1] if "\n" in summary else summary,
    })


async def _git_fetch(args: dict[str, Any], ctx: ToolContext, repo: GitRepo) -> ToolResult:
    inp = GitFetchArgs.model_validate(args)
    if not _SAFE_REMOTE_RE.match(inp.remote):
        raise ValueError(f"invalid remote name: {inp.remote}")
    if not inp.all:
        remote_check = await _run_git(repo, ["remote", "get-url", inp.remote], read_only=True)
        if remote_check["returncode"] != 0:
            return _result("fetch", ctx, repo=repo, ok=False, error="remote_not_found")
    argv = ["fetch"]
    if inp.prune:
        argv.append("--prune")
    if inp.all:
        argv.append("--all")
        proc = await _run_git(repo, argv, timeout=GIT_REMOTE_TIMEOUT_SECONDS)
    else:
        argv.append(inp.remote)
        if inp.branch:
            if not _BRANCH_NAME_RE.match(inp.branch) or _BRANCH_NAME_DENY.search(inp.branch):
                raise ValueError(f"invalid branch name: {inp.branch}")
            argv.append(inp.branch)
        proc = await _run_git(repo, argv, timeout=GIT_REMOTE_TIMEOUT_SECONDS)
    if proc["returncode"] != 0:
        stderr = proc["stderr"] or proc["stdout"]
        if "not found" in stderr.lower() or "does not appear" in stderr.lower() or "no such remote" in stderr.lower():
            return _result("fetch", ctx, repo=repo, ok=False, error="remote_not_found")
        return _result("fetch", ctx, repo=repo, ok=False, error=stderr)
    summary = proc["stdout"].strip() or proc["stderr"].strip() or "Already up to date."
    return _result("fetch", ctx, repo=repo, data={
        "remote": inp.remote, "summary": summary.split("\n")[-1] if "\n" in summary else summary,
    })


async def _git_merge(args: dict[str, Any], ctx: ToolContext, repo: GitRepo) -> ToolResult:
    inp = GitMergeArgs.model_validate(args)
    if not _SAFE_REF_RE.match(inp.branch):
        raise ValueError(f"invalid ref: {inp.branch}")
    argv = ["merge"]
    if inp.no_ff:
        argv.append("--no-ff")
    if inp.message:
        argv.extend(["-m", inp.message])
    argv.append(inp.branch)
    proc = await _run_git(repo, argv, timeout=GIT_REMOTE_TIMEOUT_SECONDS)
    if proc["returncode"] != 0:
        stderr = proc["stderr"] or proc["stdout"]
        combined = proc["stdout"] + stderr
        if "CONFLICT" in combined:
            conflicts = _parse_conflicts(combined)
            return _result("merge", ctx, repo=repo, ok=False, error="merge_conflict",
                           data={"branch": inp.branch, "conflicts": conflicts})
        if "not found" in stderr.lower() or "not something we can merge" in stderr.lower():
            return _result("merge", ctx, repo=repo, ok=False, error="branch_not_found")
        return _result("merge", ctx, repo=repo, ok=False, error=stderr)
    fast_forward = "Fast-forward" in proc["stdout"]
    rev = await _run_git(repo, ["rev-parse", "--short", "HEAD"], read_only=True)
    commit_hash = rev["stdout"].strip() if rev["returncode"] == 0 else ""
    return _result("merge", ctx, repo=repo, data={
        "branch": inp.branch, "fast_forward": fast_forward, "hash": commit_hash, "conflicts": [],
    })


async def _git_rebase(args: dict[str, Any], ctx: ToolContext, repo: GitRepo) -> ToolResult:
    inp = GitRebaseArgs.model_validate(args)
    argv = ["rebase"]
    if inp.abort:
        argv.append("--abort")
        proc = await _run_git(repo, argv)
        if proc["returncode"] != 0:
            return _result("rebase", ctx, repo=repo, ok=False, error=proc["stderr"] or proc["stdout"])
        return _result("rebase", ctx, repo=repo, data={"aborted": True})
    if inp.continue_rebase:
        argv.append("--continue")
        proc = await _run_git(repo, argv)
        if proc["returncode"] != 0:
            combined = proc["stdout"] + (proc["stderr"] or "")
            if "CONFLICT" in combined:
                conflicts = _parse_conflicts(combined)
                return _result("rebase", ctx, repo=repo, ok=False, error="rebase_conflict",
                               data={"branch": inp.branch, "conflicts": conflicts})
            return _result("rebase", ctx, repo=repo, ok=False, error=proc["stderr"] or proc["stdout"])
        return _result("rebase", ctx, repo=repo, data={"branch": inp.branch, "summary": "Rebase continued successfully"})
    if inp.onto:
        if not _SAFE_REF_RE.match(inp.onto):
            raise ValueError(f"invalid ref: {inp.onto}")
        argv.extend(["--onto", inp.onto])
    if inp.branch:
        if not _BRANCH_NAME_RE.match(inp.branch) or _BRANCH_NAME_DENY.search(inp.branch):
            raise ValueError(f"invalid branch name: {inp.branch}")
        argv.append(inp.branch)
    proc = await _run_git(repo, argv)
    if proc["returncode"] != 0:
        combined = proc["stdout"] + (proc["stderr"] or "")
        if "CONFLICT" in combined:
            conflicts = _parse_conflicts(combined)
            return _result("rebase", ctx, repo=repo, ok=False, error="rebase_conflict",
                           data={"branch": inp.branch, "conflicts": conflicts,
                                 "suggestion": "use abort=True to cancel or continue_rebase=True after resolving"})
        stderr = proc["stderr"] or proc["stdout"]
        if "not found" in stderr.lower() or "invalid reference" in stderr.lower():
            return _result("rebase", ctx, repo=repo, ok=False, error="branch_not_found")
        return _result("rebase", ctx, repo=repo, ok=False, error=stderr)
    return _result("rebase", ctx, repo=repo, data={
        "branch": inp.branch, "onto": inp.onto, "summary": "Rebased successfully",
    })


def _parse_conflicts(output: str) -> list[str]:
    conflicts = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("CONFLICT"):
            parts = stripped.split()
            if len(parts) >= 3:
                conflicts.append(parts[-1])
    return conflicts


async def _discover_repo(ctx: ToolContext) -> GitRepo | None:
    proc = await _run_process(["git", "rev-parse", "--show-toplevel"], cwd=ctx.workspace, read_only=True)
    if proc["returncode"] != 0:
        return None
    return GitRepo(
        repo_root=str(Path(proc["stdout"].strip()).resolve()),
        workspace=str(Path(ctx.workspace).resolve()),
    )


async def _run_git(repo: GitRepo, args: list[str], *, read_only: bool = False, timeout: int | None = None) -> dict[str, Any]:
    return await _run_process(["git", *args], cwd=repo.repo_root, read_only=read_only, timeout=timeout)


async def _run_process(args: list[str], *, cwd: str, read_only: bool = False, timeout: int | None = None) -> dict[str, Any]:
    effective_timeout = timeout or GIT_TIMEOUT_SECONDS
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
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=effective_timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"returncode": -1, "stdout": "", "stderr": f"git command timed out after {effective_timeout}s"}
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
        summary=f"git {command}: {'ok' if ok else 'failed'}",
        metadata={
            "command": command,
            "ok": ok,
            "error": not ok,
            "error_message": error.strip(),
        },
    )
