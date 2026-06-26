"""Git command route hints — suggest structured git tool over raw bash."""

from __future__ import annotations

from voidx.tools.bash.core import (
    RouteHint,
    _git_subcommand,
)


def _hint_git(stripped: str, words: list[str]) -> RouteHint | None:
    subcommand, _ = _git_subcommand(words)
    if not subcommand:
        return None
    git_args = stripped[len("git"):].strip()
    llm_hint = f"Prefer git tool with args={git_args!r} for structured output."
    return RouteHint(
        tool_id="git", ui_label="→ git",
        llm_hint=llm_hint,
    )
