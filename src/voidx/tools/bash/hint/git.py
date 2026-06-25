"""Git command route hints — suggest structured git tool over raw bash."""

from __future__ import annotations

from voidx.tools.bash.core import (
    RouteHint,
    _git_subcommand,
    _UNHINTABLE_GIT_SUBCOMMANDS,
)


def _hint_git(stripped: str, words: list[str]) -> RouteHint | None:
    subcommand, rest = _git_subcommand(words)
    if not subcommand:
        return None
    if subcommand in _UNHINTABLE_GIT_SUBCOMMANDS:
        return None
    git_args = stripped[len("git"):].strip()
    if '"' in git_args:
        return None
    return RouteHint(
        tool_id="git", ui_label="→ git",
        llm_hint=f'Prefer git tool with args="{git_args}" for structured output.',
    )