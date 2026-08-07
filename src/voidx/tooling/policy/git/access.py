"""Git path resolution, permission checks, and config safety validation."""

from __future__ import annotations

import os
from pathlib import Path

from voidx.tooling.policy.git.policy import GitRuntimeAccessPlan
from voidx.tooling.domain.grants import AccessGrants
from voidx.tooling.policy.filesystem.grants import resolve_access
from voidx.tooling.domain.context import ToolExecutionContext as ToolContext


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
    if ctx.authorization_service.access_grants is not None:
        return ctx.authorization_service.access_grants()
    return AccessGrants.from_parts(
        readable_files=ctx.authorization_service.read_files,
        readable_dirs=ctx.authorization_service.read_dirs,
        writable_files=ctx.authorization_service.write_files,
        writable_dirs=ctx.authorization_service.write_dirs,
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


def _external_repo_root_error(path: str, original_ctx: ToolContext, effective_ctx: ToolContext, repo_root: str) -> str:
    if not path or path == ".":
        return ""
    requested = Path(effective_ctx.workspace).resolve()
    original_workspace = Path(original_ctx.workspace).resolve()
    if _contains(original_workspace, requested):
        return ""
    if requested != Path(repo_root).resolve():
        return "git_policy_denied: external path must be repository root"
    return ""


def validate_runtime_access_plan(ctx: ToolContext, plan: GitRuntimeAccessPlan) -> str:
    if _has_unplanned_git_config_environment():
        return "git_policy_denied: unplanned config origin"
    if plan.requires_external_authorization(ctx.workspace, _access_grants(ctx)):
        return "git_policy_denied: runtime access plan requires authorization"
    if _plan_has_dangerous_config(plan):
        return "git_policy_denied: dangerous executable config"
    return ""


def _has_unplanned_git_config_environment() -> bool:
    return any(name in os.environ for name in {"GIT_CONFIG", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_COUNT"})


def alternate_object_dirs(objects: Path) -> list[Path]:
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


def discover_config_files(git_dir: Path, common_dir: Path) -> list[Path]:
    candidates = [
        git_dir / "config",
        common_dir / "config",
        git_dir / "config.worktree",
        common_dir / "config.worktree",
    ]
    return [path.resolve(strict=False) for path in candidates if path.exists()]


def discover_included_config_files(config_files: list[Path]) -> list[Path]:
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
