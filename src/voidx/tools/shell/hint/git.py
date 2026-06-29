"""Git command route hints — suggest structured git tool over raw shell.

Shared by bash and powershell: git command syntax is identical across platforms.
"""

from __future__ import annotations

from voidx.tools.shell.common import RouteHint

_GIT_GLOBAL_OPTIONS_WITH_VALUE = frozenset({
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
})


def _git_subcommand(words: list[str]) -> tuple[str, list[str]]:
    index = 1
    while index < len(words):
        word = words[index]
        if word in _GIT_GLOBAL_OPTIONS_WITH_VALUE:
            index += 2
            continue
        if any(word.startswith(f"{option}=") for option in _GIT_GLOBAL_OPTIONS_WITH_VALUE if option.startswith("--")):
            index += 1
            continue
        if word == "--":
            index += 1
            continue
        if word.startswith("-"):
            index += 1
            continue
        return word, words[index + 1:]
    return "", []


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
