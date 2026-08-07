"""Build Git runtime access plans using repository subprocess probes."""

from __future__ import annotations

from pathlib import Path

from voidx.tooling.builtin.git.models import GitProcessTimeout, GitRepo
from voidx.tooling.builtin.git.process import run_process
from voidx.tooling.policy.git.access import (
    alternate_object_dirs,
    discover_config_files,
    discover_included_config_files,
)
from voidx.tooling.policy.git.policy import GitRuntimeAccessPlan


async def runtime_access_plan(repo: GitRepo) -> GitRuntimeAccessPlan:
    worktree = Path(repo.repo_root).resolve()
    git_dir = await _git_resolved_path(repo, ["rev-parse", "--git-dir"], worktree / ".git")
    common_dir = await _git_resolved_path(repo, ["rev-parse", "--git-common-dir"], git_dir)
    index = await _git_resolved_path(repo, ["rev-parse", "--git-path", "index"], git_dir / "index")
    objects = await _git_resolved_path(repo, ["rev-parse", "--git-path", "objects"], common_dir / "objects")
    object_dirs = [objects, *alternate_object_dirs(objects)]
    discovered_configs = discover_config_files(git_dir, common_dir)
    discovered_configs.extend(discover_included_config_files(discovered_configs))
    return GitRuntimeAccessPlan(
        worktree=worktree,
        git_dir=git_dir,
        common_dir=common_dir,
        index=index,
        object_dirs=tuple(object_dirs),
        config_files=tuple(discovered_configs),
    )


async def _git_resolved_path(repo: GitRepo, args: list[str], fallback: Path) -> Path:
    proc = await run_process(["git", *args], cwd=repo.repo_root, read_only=True)
    if proc.get("timeout"):
        raise GitProcessTimeout(proc)
    raw = proc["stdout"].strip() if proc["returncode"] == 0 else ""
    if not raw:
        return fallback.resolve(strict=False)
    path = Path(raw)
    if path.is_absolute():
        return path.resolve(strict=False)
    return (Path(repo.repo_root) / path).resolve(strict=False)
