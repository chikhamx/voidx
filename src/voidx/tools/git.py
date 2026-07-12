"""Structured Git tool with raw args string and whitelist routing."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
import re
import shlex
from pathlib import Path
from typing import Any

from voidx.permission.git_policy import GitRuntimeAccessPlan, git_policy_for_args
from voidx.permission.grants import AccessGrants, resolve_access

from pydantic import BaseModel, Field

from voidx.runtime.processes import (
    create_owned_subprocess_exec,
    finalize_process_tree,
    release_owned_process,
)
from voidx.tools.base import (
    BaseTool,
    ToolContext,
    ToolResult,
    model_to_json_schema,
    _resolve_tool_path,
    _sandbox_paths_for_access,
    tool_timeout_metadata,
)
from voidx.logging.tool_log import log_tool_event


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

# Subcommands that get structured JSON output via dedicated parsers.
_STRUCTURED_SUBCOMMANDS = frozenset({
    "status", "diff", "log", "blame", "show", "branch", "remote", "tag", "stash",
})

# Subcommands that are always denied (destructive / irreversible).
_DENIED_SUBCOMMANDS = frozenset({
    "filter-branch", "gc", "prune", "fsck",
})

# Subcommand + flag combinations that are denied (destructive / irreversible).
# Maps subcommand to a set of flags; if any flag is present, the command is denied.
_DENIED_SUBCOMMAND_FLAGS: dict[str, set[str]] = {
    "reset": {"--hard"},
    "clean": {"-x", "--force"},
    "reflog": {"expire", "--expire", "--all", "--rewrite"},
}
# Subcommands where any denied short flag (even standalone) triggers denial.
# For clean: -x removes ignored files, -d removes untracked directories.
_DENIED_SHORT_FLAGS: dict[str, set[str]] = {
    "clean": {"x", "d"},
}

# Subcommands that are always read-only (no approval needed).
_READ_ONLY_SUBCOMMANDS = frozenset({
    "status", "log", "diff", "show", "blame", "rev-parse", "rev-list",
    "ls-files", "ls-tree", "describe", "shortlog", "cherry",
    "whatchanged", "notes", "grep", "cat-file", "name-rev", "for-each-ref",
})

# Write flags for branch/tag subcommands.
_REF_WRITE_FLAGS = {"-d", "-D", "-m", "-M", "--delete", "--move", "--force"}


class GitInput(BaseModel):
    path: str = Field(
        default="",
        description="Repository path to run git in; relative paths are resolved from the workspace, empty uses the workspace root.",
    )
    args: str = Field(
        min_length=1,
        description='Git subcommand and arguments only; do not include the git executable, e.g. "status --porcelain" or "log --oneline -5".',
    )


class GitRepo(BaseModel):
    repo_root: str
    workspace: str


class GitProcessTimeout(RuntimeError):
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        super().__init__(result.get("stderr") or "git command timed out")


class GitTool(BaseTool):
    id = "git"
    description = (
        "Run explicit path-scoped git operations. Pass args as a raw git subcommand string. "
        "Core read-only commands return structured JSON; other allowed commands return raw stdout. "
        "Write commands require approval; destructive commands are denied."
    )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(GitInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            inp = GitInput.model_validate(args)
        except Exception as exc:
            return _result("unknown", ctx, ok=False, error=f"invalid_args: {exc}")
        effective_ctx = _resolve_path_context(inp.path, ctx)
        if effective_ctx is None:
            return _result("unknown", ctx, ok=False, error="unsafe_path: path escapes workspace")
        explicit_root_error = _external_requested_repo_root_error(inp.path, ctx, effective_ctx)
        if explicit_root_error:
            return _result("unknown", effective_ctx, ok=False, error=explicit_root_error)
        try:
            repo = await _discover_repo(effective_ctx)
        except GitProcessTimeout as exc:
            return _timeout_result("discover", effective_ctx, exc.result)
        if repo is None:
            return _result("unknown", ctx, ok=False, error="not_a_git_repository")

        try:
            tokens = shlex.split(inp.args)
        except ValueError as exc:
            return _result("unknown", ctx, repo=repo, ok=False, error=f"invalid_args: {exc}")
        if not tokens:
            return _result("unknown", ctx, repo=repo, ok=False, error="invalid_args: empty command")

        subcommand = tokens[0]
        rest = tokens[1:]

        if subcommand in _DENIED_SUBCOMMANDS:
            return _result(subcommand, ctx, repo=repo, ok=False, error=f"command_denied: subcommand '{subcommand}' is destructive and not allowed")

        denied_flags = _DENIED_SUBCOMMAND_FLAGS.get(subcommand)
        if denied_flags and _has_denied_flag(subcommand, rest, denied_flags):
            return _result(subcommand, ctx, repo=repo, ok=False, error=f"command_denied: destructive flag in '{subcommand}'")

        policy = git_policy_for_args({"args": inp.args})
        if not policy.allowed:
            return _result(policy.subcommand or subcommand, effective_ctx, repo=repo, ok=False, error=f"git_policy_denied: {policy.reason}")
        subcommand = policy.subcommand
        rest = list(policy.rest)

        root_error = _external_repo_root_error(inp.path, ctx, effective_ctx, repo)
        if root_error:
            return _result(subcommand, effective_ctx, repo=repo, ok=False, error=root_error)
        plan_error = await _validate_runtime_access_plan(effective_ctx, repo)
        if plan_error:
            return _result(subcommand, effective_ctx, repo=repo, ok=False, error=plan_error)

        try:
            handler = _STRUCTURED_HANDLERS.get(subcommand)
            if handler is not None and _is_structured_route(subcommand, rest):
                return await handler(rest, effective_ctx, repo)
            return await _git_raw(subcommand, rest, effective_ctx, repo)
        except GitProcessTimeout as exc:
            return _timeout_result(subcommand, effective_ctx, exc.result, repo=repo)
        except ValueError as exc:
            from pydantic import ValidationError as _VE
            if isinstance(exc, _VE):
                detail = "; ".join(e.get("msg", str(e)) for e in exc.errors())
                return _result(subcommand, ctx, repo=repo, ok=False, error=f"Invalid argument: {detail}")
            return _result(subcommand, ctx, repo=repo, ok=False, error=str(exc))
        except Exception as exc:
            return _result(subcommand, ctx, repo=repo, ok=False, error=str(exc))


def _is_structured_route(subcommand: str, rest: list[str]) -> bool:
    """Check if a subcommand+args combination should use structured output."""
    if subcommand == "branch":
        # branch with no args or list flags → structured; -d/-D/-m/-M → raw
        if not rest or all(a in ("-a", "--all", "-v", "--verbose", "-l", "--list") or a.startswith("--format") for a in rest):
            return True
        return not any(a in _REF_WRITE_FLAGS for a in rest)
    if subcommand == "tag":
        # tag with no args or -l/--list → structured; -d → raw
        if not rest:
            return True
        return not any(a in ("-d", "--delete") for a in rest)
    if subcommand == "stash":
        # stash list → structured; stash push/pop/drop → raw
        if not rest:
            return False
        return rest[0] == "list"
    if subcommand == "remote":
        # remote -v / remote (no args) → structured; remote add/remove → raw
        if not rest:
            return True
        return all(a in ("-v", "--verbose") for a in rest)
    return True


def _has_denied_flag(subcommand: str, rest: list[str], denied_flags: set[str]) -> bool:
    """Check if a subcommand's args contain a denied destructive flag.

    Handles combined short flags (e.g. ``-fdx`` split into ``-f``, ``-d``,
    ``-x``) by expanding rest tokens into single-char flags and checking
    against ``_DENIED_SHORT_FLAGS``.
    """
    for flag in rest:
        if flag in denied_flags:
            return True
    denied_short = _DENIED_SHORT_FLAGS.get(subcommand)
    if denied_short:
        for flag in rest:
            if flag.startswith("-") and not flag.startswith("--"):
                for ch in flag[1:]:
                    if ch in denied_short:
                        return True
    if subcommand == "reflog" and rest and rest[0] == "expire":
        return True
    return False


# Write subcommands whose pathspec arguments must be validated against workspace.
_PATHSPEC_WRITE_SUBCOMMANDS = frozenset({
    "add", "restore", "checkout", "rm", "mv", "reset",
})


async def _git_raw(subcommand: str, rest: list[str], ctx: ToolContext, repo: GitRepo) -> ToolResult:
    """Execute a git command and return raw stdout/stderr/returncode."""
    is_read = _is_read_only_subcommand(subcommand, rest)
    timeout = GIT_TIMEOUT_SECONDS
    if subcommand in ("push", "pull", "fetch"):
        timeout = GIT_REMOTE_TIMEOUT_SECONDS

    if not is_read and subcommand in _PATHSPEC_WRITE_SUBCOMMANDS:
        try:
            rest = _sanitize_raw_pathspecs(rest, ctx, repo)
        except ValueError as exc:
            return _result(subcommand, ctx, repo=repo, ok=False, error=str(exc))

    proc = await _run_git(repo, [subcommand, *rest], read_only=is_read, timeout=timeout)
    ok = proc["returncode"] == 0
    return _result(subcommand, ctx, repo=repo, ok=ok, data={
        "stdout": proc["stdout"],
        "stderr": proc["stderr"],
        "returncode": proc["returncode"],
    }, error="" if ok else (proc["stderr"] or proc["stdout"]))


def _sanitize_raw_pathspecs(rest: list[str], ctx: ToolContext, repo: GitRepo) -> list[str]:
    """Validate pathspec tokens after -- in raw write commands.

    For commands like ``add -- path1 path2``, the paths after ``--`` are
    resolved and checked against the workspace boundary. Non-pathspec tokens
    (flags, refs) are passed through unchanged.
    """
    if "--" not in rest:
        return rest
    idx = rest.index("--")
    before = rest[:idx]
    path_tokens = rest[idx + 1:]
    if not path_tokens:
        return rest
    validated = _pathspecs(path_tokens, ctx, repo, allow_empty=False)
    return [*before, "--", *validated]


def _is_read_only_subcommand(subcommand: str, rest: list[str]) -> bool:
    """Classify a git subcommand+args as read-only or write."""
    args = " ".join([shlex.quote(subcommand), *(shlex.quote(arg) for arg in rest)])
    decision = git_policy_for_args({"args": args})
    return decision.allowed and decision.read_only


def is_git_read_only(args: dict) -> bool:
    """Classify a registered git tool call (raw args dict) as read-only or write."""
    decision = git_policy_for_args(args)
    return decision.allowed and decision.read_only


def _extract_pathspec(rest: list[str]) -> list[str]:
    """Extract pathspec after -- from shlex tokens."""
    if "--" in rest:
        idx = rest.index("--")
        return rest[idx + 1:]
    return []


def _extract_flag_value(rest: list[str], flag: str) -> str | None:
    """Extract --flag=value or --flag value from tokens."""
    for i, token in enumerate(rest):
        if token == flag and i + 1 < len(rest):
            return rest[i + 1]
        if token.startswith(f"{flag}="):
            return token.split("=", 1)[1]
    return None


def _has_flag(rest: list[str], *flags: str) -> bool:
    return any(f in rest for f in flags)


async def _git_status(rest: list[str], ctx: ToolContext, repo: GitRepo) -> ToolResult:
    pathspec = _pathspecs(_extract_pathspec(rest), ctx, repo, allow_empty=True)
    proc = await _run_git(repo, ["status", "--porcelain=v1", "-z", "--", *pathspec], read_only=True)
    if proc["returncode"] != 0:
        return _result("status", ctx, repo=repo, ok=False, error=proc["stderr"] or proc["stdout"])
    entries = _parse_status(proc["stdout"], repo, ctx.workspace)
    branch_proc = await _run_git(repo, ["symbolic-ref", "--short", "HEAD"], read_only=True)
    branch = branch_proc["stdout"].strip() if branch_proc["returncode"] == 0 else ""
    return _result("status", ctx, repo=repo, data={"entries": entries, "branch": branch})


async def _git_diff(rest: list[str], ctx: ToolContext, repo: GitRepo) -> ToolResult:
    cached = _has_flag(rest, "--cached", "--staged")
    pathspec = _pathspecs(_extract_pathspec(rest), ctx, repo, allow_empty=True)
    pre_dash = rest[:rest.index("--")] if "--" in rest else rest
    refs = [t for t in pre_dash if not t.startswith("-") and t not in ("--cached", "--staged")]
    base = refs[0] if len(refs) >= 2 else ""
    ref = refs[-1] if refs else ""

    base_argv = ["diff"]
    if cached:
        base_argv.append("--cached")
    if base and ref:
        base_argv.extend([base, ref])
    elif ref:
        base_argv.append(ref)
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


async def _git_log(rest: list[str], ctx: ToolContext, repo: GitRepo) -> ToolResult:
    limit = 10
    limit_note = ""
    author = _extract_flag_value(rest, "--author") or ""
    since = _extract_flag_value(rest, "--since") or ""
    until = _extract_flag_value(rest, "--until") or ""
    path = ""

    skip_next = False
    for i, token in enumerate(rest):
        if skip_next:
            skip_next = False
            continue
        if token == "-n" and i + 1 < len(rest):
            try:
                limit = min(int(rest[i + 1]), LOG_LIMIT_MAX)
            except ValueError:
                limit_note = f"invalid -n value '{rest[i + 1]}', defaulted to {limit}"
            skip_next = True
        elif token.startswith("-n"):
            try:
                limit = min(int(token[2:]), LOG_LIMIT_MAX)
            except ValueError:
                limit_note = f"invalid -n value '{token[2:]}', defaulted to {limit}"
        elif token.startswith("-") and len(token) > 1 and token[1:].isdigit():
            try:
                limit = min(int(token[1:]), LOG_LIMIT_MAX)
            except ValueError:
                limit_note = f"invalid -n value '{token[1:]}', defaulted to {limit}"
        elif token == "--" and i + 1 < len(rest):
            path = rest[i + 1]
            break
        elif not token.startswith("-") and not path:
            path = token

    argv = [
        "log", f"-n{limit}", "--name-only",
        "--pretty=format:%H%x1f%an%x1f%ad%x1f%s",
        "--date=iso-strict",
    ]
    if author:
        argv.append(f"--author={author}")
    if since:
        argv.append(f"--since={since}")
    if until:
        argv.append(f"--until={until}")
    if path:
        argv.extend(["--", *_pathspecs([path], ctx, repo, allow_empty=False)])
    proc = await _run_git(repo, argv, read_only=True)
    if proc["returncode"] != 0:
        return _result("log", ctx, repo=repo, ok=False, error=proc["stderr"] or proc["stdout"])
    data = {"entries": _parse_log(proc["stdout"], repo, ctx.workspace)}
    if limit_note:
        data["limit_note"] = limit_note
    return _result("log", ctx, repo=repo, data=data)


async def _git_blame(rest: list[str], ctx: ToolContext, repo: GitRepo) -> ToolResult:
    path = None
    start = None
    end = None
    i = 0
    while i < len(rest):
        token = rest[i]
        if token == "-L" and i + 1 < len(rest):
            parts = rest[i + 1].split(",", 1)
            if len(parts) == 2:
                try:
                    start = int(parts[0])
                    end = int(parts[1])
                except ValueError:
                    pass
            i += 2
        elif token.startswith("-"):
            i += 1
        elif path is None:
            path = token
            i += 1
        else:
            i += 1
    if path is None:
        return _result("blame", ctx, repo=repo, ok=False, error="path is required")
    if end is not None and start is not None and end < start:
        return _result("blame", ctx, repo=repo, ok=False, error="blame end must be >= start")
    if start is not None and end is not None and end - start + 1 > BLAME_RANGE_MAX:
        return _result("blame", ctx, repo=repo, ok=False, error=f"blame range must be at most {BLAME_RANGE_MAX} lines")
    repo_path = _pathspecs([path], ctx, repo, allow_empty=False)[0]
    argv = ["blame", "--line-porcelain"]
    if start is not None:
        end_val = end or start
        argv.extend([f"-L{start},{end_val}"])
    argv.extend(["--", repo_path])
    proc = await _run_git(repo, argv, read_only=True)
    if proc["returncode"] != 0:
        return _result("blame", ctx, repo=repo, ok=False, error=proc["stderr"] or proc["stdout"])
    return _result("blame", ctx, repo=repo, data={"entries": _parse_blame(proc["stdout"])})


async def _git_branch_list(rest: list[str], ctx: ToolContext, repo: GitRepo) -> ToolResult:
    argv = ["branch"]
    if _has_flag(rest, "-a", "--all"):
        argv.append("--all")
    argv.extend(["--format=%(refname:short)\t%(HEAD)\t%(upstream:short)\t%(upstream:track)"])
    proc = await _run_git(repo, argv, read_only=True)
    if proc["returncode"] != 0:
        return _result("branch", ctx, repo=repo, ok=False, error=proc["stderr"] or proc["stdout"])
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
    return _result("branch", ctx, repo=repo, data={"entries": entries})


async def _git_remote_list(rest: list[str], ctx: ToolContext, repo: GitRepo) -> ToolResult:
    proc = await _run_git(repo, ["remote", "-v"], read_only=True)
    if proc["returncode"] != 0:
        return _result("remote", ctx, repo=repo, ok=False, error=proc["stderr"] or proc["stdout"])
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
    return _result("remote", ctx, repo=repo, data={"entries": entries})


async def _git_show(rest: list[str], ctx: ToolContext, repo: GitRepo) -> ToolResult:
    pathspec = _pathspecs(_extract_pathspec(rest), ctx, repo, allow_empty=True)
    stat = _has_flag(rest, "--stat")
    pre_dash = rest[:rest.index("--")] if "--" in rest else rest
    refs = [t for t in pre_dash if not t.startswith("-")]
    ref = refs[0] if refs else "HEAD"

    meta_argv = ["show", f"--format=%H%x1f%an%x1f%ad%x1f%s%x1f%P", "--no-patch", ref]
    meta_proc = await _run_git(repo, meta_argv, read_only=True)
    if meta_proc["returncode"] != 0:
        return _result("show", ctx, repo=repo, ok=False, error=meta_proc["stderr"].strip() or meta_proc["stdout"].strip() or "ref_not_found")
    meta_line = meta_proc["stdout"].strip()
    parts = meta_line.split("\x1f")
    if len(parts) < 4:
        return _result("show", ctx, repo=repo, ok=False, error="failed to parse commit metadata")
    commit_hash, author, date, message = parts[0], parts[1], parts[2], parts[3]
    parents = parts[4].split() if len(parts) > 4 and parts[4] else []
    is_merge = len(parents) > 1
    if stat:
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


async def _git_tag_list(rest: list[str], ctx: ToolContext, repo: GitRepo) -> ToolResult:
    argv = ["tag", "-l", "--format=%(refname:short) %(objectname:short)"]
    pattern = ""
    sort = _extract_flag_value(rest, "--sort") or ""
    for token in rest:
        if not token.startswith("-") and token not in ("-l", "--list"):
            pattern = token
            break
    if pattern:
        argv.append(pattern)
    if sort:
        argv.append(f"--sort={sort}")
    proc = await _run_git(repo, argv, read_only=True)
    if proc["returncode"] != 0:
        return _result("tag", ctx, repo=repo, ok=False, error=proc["stderr"] or proc["stdout"])
    entries = []
    for line in proc["stdout"].splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        tag_name = parts[0]
        tag_hash = parts[1] if len(parts) > 1 else ""
        entries.append({"name": tag_name, "hash": tag_hash})
    return _result("tag", ctx, repo=repo, data={"entries": entries})


async def _git_stash_list(rest: list[str], ctx: ToolContext, repo: GitRepo) -> ToolResult:
    proc = await _run_git(repo, ["stash", "list", "--format=%gd%x1f%s%x1f%cr"], read_only=True)
    if proc["returncode"] != 0:
        return _result("stash", ctx, repo=repo, ok=False, error=proc["stderr"] or proc["stdout"])
    entries = []
    for line in proc["stdout"].splitlines():
        if not line:
            continue
        parts = line.split("\x1f")
        if len(parts) >= 3:
            entries.append({"ref": parts[0], "message": parts[1], "date": parts[2]})
        elif len(parts) >= 2:
            entries.append({"ref": parts[0], "message": parts[1], "date": ""})
    return _result("stash", ctx, repo=repo, data={"entries": entries})


_STRUCTURED_HANDLERS: dict[str, Any] = {
    "status": _git_status,
    "diff": _git_diff,
    "log": _git_log,
    "blame": _git_blame,
    "show": _git_show,
    "branch": _git_branch_list,
    "remote": _git_remote_list,
    "tag": _git_tag_list,
    "stash": _git_stash_list,
}


def _parse_conflicts(output: str) -> list[str]:
    conflicts = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("CONFLICT"):
            parts = stripped.split()
            if len(parts) >= 3:
                conflicts.append(parts[-1])
    return conflicts

def _resolve_path_context(path: str, ctx: ToolContext) -> ToolContext | None:
    """Return a ToolContext with workspace adjusted to inp.path."""
    if not path or path == ".":
        return ctx
    grants = _access_grants(ctx)
    resolution = resolve_access(
        ctx.workspace,
        path,
        access="read",
        access_grants=grants,
        require_exists=True,
    )
    if resolution.action != "allow" or resolution.intent is None:
        return None
    return ctx.model_copy(update={"workspace": str(resolution.intent.normalized_path)})


def _access_grants(ctx: ToolContext) -> AccessGrants:
    if ctx.get_access_grants is not None:
        return ctx.get_access_grants()
    return AccessGrants.from_parts(
        readable_files=ctx.sandbox_readable_files,
        readable_dirs=ctx.sandbox_readable_dirs,
        writable_files=ctx.sandbox_writable_files,
        writable_dirs=ctx.sandbox_writable_dirs,
    )


def _contains(base: Path, path: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _external_requested_repo_root_error(path: str, original_ctx: ToolContext, effective_ctx: ToolContext) -> str:
    if not path or path == ".":
        return ""
    requested = Path(effective_ctx.workspace).resolve()
    original_workspace = Path(original_ctx.workspace).resolve()
    if _contains(original_workspace, requested):
        return ""
    if _looks_like_worktree_root(requested) or _looks_like_bare_git_dir(requested):
        return ""
    return "git_policy_denied: external path must be repository root"


def _looks_like_worktree_root(path: Path) -> bool:
    return (path / ".git").is_dir() or (path / ".git").is_file()


def _looks_like_bare_git_dir(path: Path) -> bool:
    return (path / "HEAD").is_file() and (path / "objects").is_dir() and ((path / "refs").is_dir() or (path / "packed-refs").exists())


def _external_repo_root_error(path: str, original_ctx: ToolContext, effective_ctx: ToolContext, repo: GitRepo) -> str:
    if not path or path == ".":
        return ""
    requested = Path(effective_ctx.workspace).resolve()
    original_workspace = Path(original_ctx.workspace).resolve()
    if _contains(original_workspace, requested):
        return ""
    if requested != Path(repo.repo_root).resolve():
        return "git_policy_denied: external path must be repository root"
    return ""


async def _validate_runtime_access_plan(ctx: ToolContext, repo: GitRepo) -> str:
    if _has_unplanned_git_config_environment():
        return "git_policy_denied: unplanned config origin"
    plan = await _runtime_access_plan(repo)
    if plan.requires_external_authorization(ctx.workspace, _access_grants(ctx)):
        return "git_policy_denied: runtime access plan requires authorization"
    if _plan_has_dangerous_config(plan):
        return "git_policy_denied: dangerous executable config"
    return ""


def _has_unplanned_git_config_environment() -> bool:
    return any(name in os.environ for name in {"GIT_CONFIG", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_COUNT"})


async def _runtime_access_plan(repo: GitRepo) -> GitRuntimeAccessPlan:
    worktree = Path(repo.repo_root).resolve()
    git_dir = await _git_resolved_path(repo, ["rev-parse", "--git-dir"], worktree / ".git")
    common_dir = await _git_resolved_path(repo, ["rev-parse", "--git-common-dir"], git_dir)
    index = await _git_resolved_path(repo, ["rev-parse", "--git-path", "index"], git_dir / "index")
    objects = await _git_resolved_path(repo, ["rev-parse", "--git-path", "objects"], common_dir / "objects")
    object_dirs = [objects, *_alternate_object_dirs(objects)]
    config_files = _config_files(git_dir, common_dir)
    config_files.extend(_included_config_files(config_files))
    return GitRuntimeAccessPlan(
        worktree=worktree,
        git_dir=git_dir,
        common_dir=common_dir,
        index=index,
        object_dirs=tuple(object_dirs),
        config_files=tuple(config_files),
    )


async def _git_resolved_path(repo: GitRepo, args: list[str], fallback: Path) -> Path:
    proc = await _run_process(["git", *args], cwd=repo.repo_root, read_only=True)
    if proc.get("timeout"):
        raise GitProcessTimeout(proc)
    raw = proc["stdout"].strip() if proc["returncode"] == 0 else ""
    if not raw:
        return fallback.resolve(strict=False)
    path = Path(raw)
    if path.is_absolute():
        return path.resolve(strict=False)
    return (Path(repo.repo_root) / path).resolve(strict=False)


def _alternate_object_dirs(objects: Path) -> list[Path]:
    alternates = objects / "info" / "alternates"
    if not alternates.exists():
        return []
    result: list[Path] = []
    try:
        lines = alternates.read_text(encoding="utf-8").splitlines()
    except OSError:
        return result
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        path = Path(stripped)
        if not path.is_absolute():
            path = alternates.parent / path
        result.append(path.resolve(strict=False))
    return result


def _config_files(git_dir: Path, common_dir: Path) -> list[Path]:
    candidates = [
        git_dir / "config",
        common_dir / "config",
        git_dir / "config.worktree",
        common_dir / "config.worktree",
    ]
    return [path.resolve(strict=False) for path in candidates if path.exists()]


def _included_config_files(config_files: list[Path]) -> list[Path]:
    result: list[Path] = []
    seen = {path.resolve(strict=False) for path in config_files}
    pending = list(config_files)
    while pending:
        config_file = pending.pop(0)
        for include in _parse_include_paths(config_file):
            resolved = include.resolve(strict=False)
            if resolved in seen:
                continue
            seen.add(resolved)
            result.append(resolved)
            if resolved.exists():
                pending.append(resolved)
    return result


def _parse_include_paths(config_file: Path) -> list[Path]:
    includes: list[Path] = []
    section = ""
    try:
        lines = config_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return includes
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped.strip("[]").split(' ', 1)[0].lower()
            continue
        if section in {"include", "includeif"} and "=" in stripped:
            key, value = stripped.split("=", 1)
            if key.strip().lower() == "path":
                raw_value = value.strip().strip('"')
                path = Path(raw_value).expanduser() if raw_value.startswith("~") else Path(raw_value)
                includes.append(path if path.is_absolute() else config_file.parent / path)
    return includes


def _plan_has_dangerous_config(plan: GitRuntimeAccessPlan) -> bool:
    dangerous_exact = {
        "core.askpass",
        "core.editor",
        "core.fsmonitor",
        "core.hookspath",
        "core.pager",
        "core.sshcommand",
        "credential.helper",
        "diff.external",
        "gpg.program",
        "gpg.ssh.program",
        "sequence.editor",
    }
    dangerous_prefixes = (
        "filter.",
        "diff.",
        "difftool.",
        "mergetool.",
    )
    for config_file in plan.config_files:
        section = ""
        try:
            lines = config_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", ";")):
                continue
            if stripped.startswith("[") and stripped.endswith("]"):
                section = stripped.strip("[]").split(' ', 1)[0].lower()
                continue
            if "=" not in stripped:
                continue
            key = stripped.split("=", 1)[0].strip().lower()
            full_key = f"{section}.{key}" if section else key
            if full_key in dangerous_exact or any(full_key.startswith(prefix) for prefix in dangerous_prefixes):
                return True
    return False


async def _discover_repo(ctx: ToolContext) -> GitRepo | None:
    proc = await _run_process(["git", "rev-parse", "--show-toplevel"], cwd=ctx.workspace, read_only=True)
    if proc.get("timeout"):
        raise GitProcessTimeout(proc)
    if proc["returncode"] != 0:
        return None
    return GitRepo(
        repo_root=str(Path(proc["stdout"].strip()).resolve()),
        workspace=str(Path(ctx.workspace).resolve()),
    )


async def _run_git(repo: GitRepo, args: list[str], *, read_only: bool = False, timeout: int | None = None) -> dict[str, Any]:
    result = await _run_process(["git", *args], cwd=repo.repo_root, read_only=read_only, timeout=timeout)
    if result.get("timeout"):
        raise GitProcessTimeout(result)
    return result


async def _run_process(args: list[str], *, cwd: str, read_only: bool = False, timeout: int | None = None) -> dict[str, Any]:
    effective_timeout = timeout or GIT_TIMEOUT_SECONDS
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    })
    if read_only:
        env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        proc = await create_owned_subprocess_exec(
            *args,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return {"returncode": -1, "stdout": "", "stderr": "git executable not found"}

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=effective_timeout)
        await release_owned_process(proc)
    except asyncio.TimeoutError:
        await finalize_process_tree(proc)
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": f"git command timed out after {effective_timeout}s",
            **tool_timeout_metadata("git"),
        }
    except asyncio.CancelledError:
        await finalize_process_tree(proc)
        raise
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
    proc = await _run_git(repo, ["diff", "--cached", "--name-only"], read_only=True)
    if proc["returncode"] != 0:
        log_tool_event("git_staged_files_failed", tool_name="git", message=f"git diff --cached failed: {proc['stderr'].strip() or proc['stdout'].strip()}")
        return []
    return [_display_path(line, repo, ctx.workspace) for line in proc["stdout"].splitlines() if line.strip()]


async def _unstaged_files(ctx: ToolContext, repo: GitRepo) -> list[str]:
    proc = await _run_git(repo, ["diff", "--name-only"], read_only=True)
    if proc["returncode"] != 0:
        log_tool_event("git_unstaged_files_failed", tool_name="git", message=f"git diff failed: {proc['stderr'].strip() or proc['stdout'].strip()}")
        return []
    return [_display_path(line, repo, ctx.workspace) for line in proc["stdout"].splitlines() if line.strip()]


async def _commit_files(ctx: ToolContext, repo: GitRepo, ref: str) -> list[str]:
    proc = await _run_git(repo, ["diff-tree", "--no-commit-id", "--name-only", "-r", ref], read_only=True)
    if proc["returncode"] != 0:
        log_tool_event("git_commit_files_failed", tool_name="git", message=f"git diff-tree {ref} failed: {proc['stderr'].strip() or proc['stdout'].strip()}")
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


def _timeout_result(
    command: str,
    ctx: ToolContext,
    process_result: dict[str, Any],
    *,
    repo: GitRepo | None = None,
) -> ToolResult:
    error = str(process_result.get("stderr") or "git command timed out").strip()
    data = {
        "stdout": str(process_result.get("stdout") or ""),
        "stderr": error,
        "returncode": int(process_result.get("returncode", -1)),
    }
    payload = {
        "ok": False,
        "command": command,
        "repo_root": repo.repo_root if repo else "",
        "workspace": repo.workspace if repo else str(Path(ctx.workspace).resolve()),
        "data": data,
        "error": error,
    }
    return ToolResult(
        title=f"git: {command}",
        output=json.dumps(payload, ensure_ascii=False, indent=2),
        summary="timed out",
        metadata=tool_timeout_metadata(
            "git",
            command=command,
            returncode=-1,
            error_message=error,
        ),
    )


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
    metadata = {
        "command": command,
        "ok": ok,
    }
    if not ok:
        metadata["error"] = True
        metadata["error_message"] = error.strip()
    return ToolResult(
        title=f"git: {command}",
        output=json.dumps(payload, ensure_ascii=False, indent=2),
        summary="ok" if ok else "failed",
        metadata=metadata,
    )
