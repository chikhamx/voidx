"""Git command route hints — suggest structured git tool over raw shell.

Shared by bash and powershell: git command syntax is identical across platforms.
"""

from __future__ import annotations

import shlex

from voidx.tools.shell.common import RouteHint

_GIT_GLOBAL_OPTIONS_WITH_VALUE = frozenset({
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
})


def _git_tool_args(words: list[str]) -> tuple[str, str]:
    path = ""
    index = 1
    while index < len(words):
        word = words[index]
        if word == "-C":
            if index + 1 >= len(words):
                return "", ""
            path = words[index + 1]
            index += 2
            continue
        if word.startswith("--git-dir=") or word.startswith("--work-tree="):
            return "", ""
        if word in {"--git-dir", "--work-tree", "--namespace", "--exec-path"}:
            return "", ""
        if word == "-c":
            index += 2
            continue
        if any(word.startswith(f"{option}=") for option in ("--namespace", "--exec-path")):
            return "", ""
        if word in {
            "--no-pager",
            "--paginate",
            "--literal-pathspecs",
            "--glob-pathspecs",
            "--noglob-pathspecs",
            "--icase-pathspecs",
        }:
            index += 1
            continue
        if word == "--":
            index += 1
            continue
        if word.startswith("-"):
            index += 1
            continue
        return path, shlex.join(words[index:])
    return "", ""


def _git_subcommand(words: list[str]) -> tuple[str, list[str]]:
    _, git_args = _git_tool_args(words)
    if not git_args:
        return "", []
    parsed = shlex.split(git_args)
    if not parsed:
        return "", []
    return parsed[0], parsed[1:]


def _hint_git(stripped: str, words: list[str]) -> RouteHint | None:
    path, git_args = _git_tool_args(words)
    if not git_args:
        return None
    if path:
        llm_hint = f"Prefer git tool with path={path!r}, args={git_args!r} for structured output."
    else:
        llm_hint = f"Prefer git tool with args={git_args!r} for structured output."
    return RouteHint(
        tool_id="git", ui_label="→ git",
        llm_hint=llm_hint,
    )
