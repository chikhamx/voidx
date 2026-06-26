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

from pydantic import BaseModel, Field

from voidx.tools.base import BaseTool, ToolContext, ToolResult, model_to_json_schema, resolve_safe
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
    path: str = Field(default="", description="Optional execution path relative to workspace. Empty uses workspace root.")
    args: str = Field(min_length=1, description='Git subcommand and arguments as a raw string, e.g. "status --porcelain" or "log --oneline -5".')


class GitRepo(BaseModel):
    repo_root: str
    workspace: str


class GitTool(BaseTool):
    id = "git"
    description = (
        "Inspect and perform explicit path-scoped Git operations with structured JSON output. "
        "Pass a raw git args string (e.g. args='status --porcelain'). "
        "Core read commands (status, diff, log, blame, show, branch list, remote, tag list, stash list) "
        "return structured JSON. All other commands return raw stdout. "
        "Write commands require approval."
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
        repo = await _discover_repo(effective_ctx)
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
            return _result(subcommand, ctx, repo=repo, ok=False, error="command_denied")

        denied_flags = _DENIED_SUBCOMMAND_FLAGS.get(subcommand)
        if denied_flags and _has_denied_flag(subcommand, rest, denied_flags):
            return _result(subcommand, ctx, repo=repo, ok=False, error="command_denied")

        try:
            handler = _STRUCTURED_HANDLERS.get(subcommand)
            if handler is not None and _is_structured_route(subcommand, rest):
                return await handler(rest, effective_ctx, repo)
            return await _git_raw(subcommand, rest, effective_ctx, repo)
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
    if subcommand in _READ_ONLY_SUBCOMMANDS:
        return True
    if subcommand == "config":
        read_flags = {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l", "--show-origin", "--show-scope"}
        if any(a in read_flags for a in rest):
            return True
        scope_flags = {"--global", "--system", "--local", "--file", "--blob"}
        value_tokens = [a for a in rest if not a.startswith("-") and a not in scope_flags]
        return len(value_tokens) <= 1
    if subcommand == "stash":
        return bool(rest) and rest[0] in ("list", "show")
    if subcommand == "reflog":
        return bool(rest) and rest[0] in ("show", "list")
    if subcommand in ("branch", "tag"):
        return not any(a in _REF_WRITE_FLAGS for a in rest)
    if subcommand == "remote":
        return not rest or all(a in ("-v", "--verbose") for a in rest)
    if subcommand == "worktree":
        return bool(rest) and rest[0] == "list"
    if subcommand == "bisect":
        return bool(rest) and rest[0] in ("log", "view", "visualize")
    return False


def is_git_read_only(args: dict) -> bool:
    """Classify a git tool call (raw args dict) as read-only or write.

    Single source of truth for git read/write classification.
    Used by both git.py internals and permission/rules.py.
    """
    raw_args = str(args.get("args", ""))
    try:
        tokens = shlex.split(raw_args)
    except ValueError:
        return False
    if not tokens:
        return False
    return _is_read_only_subcommand(tokens[0], tokens[1:])


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
    """Return a ToolContext with workspace adjusted to inp.path.

    Empty path returns the original ctx. Non-empty path is resolved against
    the current workspace and must stay inside it.
    """
    if not path or path == ".":
        return ctx
    resolved = resolve_safe(ctx.workspace, path, ctx.sandbox_extra_paths)
    if resolved is None:
        return None
    return ctx.model_copy(update={"workspace": str(resolved)})


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
