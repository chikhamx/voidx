"""Git structured subcommand handlers — produce structured JSON for core read-only commands."""

from __future__ import annotations

from typing import Any

from voidx.tooling.domain.context import ToolExecutionContext as ToolContext
from voidx.tooling.domain.result import ToolResult

from voidx.tooling.builtin.git.constants import LOG_LIMIT_MAX, BLAME_RANGE_MAX
from voidx.tooling.builtin.git.models import GitRepo
from voidx.tooling.builtin.git.parsers import (
    _pathspecs,
    _parse_status,
    _diff_hunks_by_path,
    _display_path,
    _parse_log,
    _parse_blame,
    _parse_show_numstat,
)
from voidx.tooling.builtin.git.process import run_git
from voidx.tooling.builtin.git.results import _result, _parse_track
from voidx.tooling.builtin.git.routing import (
    _extract_pathspec,
    _extract_flag_value,
    _has_flag,
)


async def _git_status(rest: list[str], ctx: ToolContext, repo: GitRepo) -> ToolResult:
    pathspec = _pathspecs(_extract_pathspec(rest), ctx, repo, allow_empty=True)
    proc = await run_git(repo, ["status", "--porcelain=v1", "-z", "--", *pathspec], read_only=True)
    if proc["returncode"] != 0:
        return _result("status", ctx, repo=repo, ok=False, error=proc["stderr"] or proc["stdout"])
    entries = _parse_status(proc["stdout"], repo, ctx.workspace)
    branch_proc = await run_git(repo, ["symbolic-ref", "--short", "HEAD"], read_only=True)
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
    proc = await run_git(repo, [*base_argv, "--numstat", "--", *pathspec], read_only=True)
    if proc["returncode"] != 0:
        return _result("diff", ctx, repo=repo, ok=False, error=proc["stderr"] or proc["stdout"])
    hunk_proc = await run_git(repo, [*base_argv, "--unified=3", "--", *pathspec], read_only=True)
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
    proc = await run_git(repo, argv, read_only=True)
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
    proc = await run_git(repo, argv, read_only=True)
    if proc["returncode"] != 0:
        return _result("blame", ctx, repo=repo, ok=False, error=proc["stderr"] or proc["stdout"])
    return _result("blame", ctx, repo=repo, data={"entries": _parse_blame(proc["stdout"])})


async def _git_branch_list(rest: list[str], ctx: ToolContext, repo: GitRepo) -> ToolResult:
    argv = ["branch"]
    if _has_flag(rest, "-a", "--all"):
        argv.append("--all")
    argv.extend(["--format=%(refname:short)\t%(HEAD)\t%(upstream:short)\t%(upstream:track)"])
    proc = await run_git(repo, argv, read_only=True)
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
    proc = await run_git(repo, ["remote", "-v"], read_only=True)
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
    meta_proc = await run_git(repo, meta_argv, read_only=True)
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
        numstat_proc = await run_git(repo, numstat_argv, read_only=True)
        if numstat_proc["returncode"] != 0:
            return _result("show", ctx, repo=repo, ok=False, error=numstat_proc["stderr"] or numstat_proc["stdout"])
        shortstat_argv = ["show", "--format=", "--shortstat", ref, "--", *pathspec]
        shortstat_proc = await run_git(repo, shortstat_argv, read_only=True)
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
    diff_proc = await run_git(repo, diff_argv, read_only=True)
    if diff_proc["returncode"] != 0:
        return _result("show", ctx, repo=repo, ok=False, error=diff_proc["stderr"] or diff_proc["stdout"])
    numstat_argv = ["show", "--format=", "--numstat", ref, "--", *pathspec]
    numstat_proc = await run_git(repo, numstat_argv, read_only=True)
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
    proc = await run_git(repo, argv, read_only=True)
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
    proc = await run_git(repo, ["stash", "list", "--format=%gd%x1f%s%x1f%cr"], read_only=True)
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
