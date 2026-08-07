"""Git subcommand routing helpers — classification, flag checks, pathspec extraction."""

from __future__ import annotations

from voidx.tooling.domain.context import ToolExecutionContext as ToolContext
from voidx.tooling.domain.result import ToolResult

from voidx.tooling.policy.git.constants import REF_WRITE_FLAGS
from voidx.tooling.policy.git.routing import is_read_only_subcommand
from voidx.tooling.builtin.git.models import GitRepo
from voidx.tooling.builtin.git.parsers import _pathspecs
from voidx.tooling.builtin.git.results import _result


def _is_structured_route(subcommand: str, rest: list[str]) -> bool:
    """Check if a subcommand+args combination should use structured output."""
    if subcommand == "branch":
        # branch with no args or list flags → structured; -d/-D/-m/-M → raw
        if not rest or all(a in ("-a", "--all", "-v", "--verbose", "-l", "--list") or a.startswith("--format") for a in rest):
            return True
        return not any(a in REF_WRITE_FLAGS for a in rest)
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


async def _git_raw(subcommand: str, rest: list[str], ctx: ToolContext, repo: GitRepo) -> ToolResult:
    """Execute a git command and return raw stdout/stderr/returncode."""
    from voidx.tooling.builtin.git.constants import GIT_REMOTE_TIMEOUT_SECONDS, GIT_TIMEOUT_SECONDS
    from voidx.tooling.policy.git.constants import PATHSPEC_WRITE_SUBCOMMANDS
    from voidx.tooling.builtin.git.process import run_git

    is_read = is_read_only_subcommand(subcommand, rest)
    timeout = GIT_TIMEOUT_SECONDS
    if subcommand in ("push", "pull", "fetch"):
        timeout = GIT_REMOTE_TIMEOUT_SECONDS

    if not is_read and subcommand in PATHSPEC_WRITE_SUBCOMMANDS:
        try:
            rest = _sanitize_raw_pathspecs(rest, ctx, repo)
        except ValueError as exc:
            return _result(subcommand, ctx, repo=repo, ok=False, error=str(exc))

    proc = await run_git(repo, [subcommand, *rest], read_only=is_read, timeout=timeout)
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
