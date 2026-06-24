"""Git command route hints — suggest structured git tool over raw bash."""

from __future__ import annotations

import re

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
    if subcommand == "commit":
        return _hint_git_commit(stripped, rest)
    mapping: dict[str, object] = {
        "status": _hint_git_status,
        "diff": _hint_git_diff,
        "log": _hint_git_log,
        "blame": _hint_git_blame,
        "branch": _hint_git_branch,
        "remote": _hint_git_remote,
        "add": _hint_git_add,
        "restore": _hint_git_restore,
        "show": _hint_git_show,
        "switch": _hint_git_switch,
        "tag": _hint_git_tag,
        "stash": _hint_git_stash,
    }
    hinter = mapping.get(subcommand)
    if hinter is None:
        return None
    return hinter(rest)  # type: ignore[operator]


def _hint_git_status(rest: list[str]) -> RouteHint | None:
    pathspec: list[str] = []
    i = 0
    while i < len(rest):
        if rest[i] == "--" and i + 1 < len(rest):
            pathspec = rest[i + 1:]
            break
        if rest[i].startswith("-"):
            return None
        pathspec.append(rest[i])
        i += 1
    if pathspec:
        return RouteHint(
            tool_id="git", ui_label="→ git",
            llm_hint=f'Prefer git(command="status", args={{"pathspec": {pathspec}}}) for structured JSON output.',
        )
    return RouteHint(
        tool_id="git", ui_label="→ git",
        llm_hint='Prefer git(command="status") for structured JSON output.',
    )


def _hint_git_diff(rest: list[str]) -> RouteHint | None:
    cached = False
    ref = ""
    base = ""
    pathspec: list[str] = []
    i = 0
    while i < len(rest):
        if rest[i] == "--cached" or rest[i] == "--staged":
            cached = True
            i += 1
        elif rest[i] == "--" and i + 1 < len(rest):
            pathspec = rest[i + 1:]
            break
        elif rest[i].startswith("-"):
            return None
        elif not ref:
            ref = rest[i]
            i += 1
        elif not base:
            base = rest[i]
            i += 1
        else:
            return None
    args_parts: list[str] = []
    if cached:
        args_parts.append('"cached": true')
    if ref:
        args_parts.append(f'"ref": "{ref}"')
    if base:
        args_parts.append(f'"base": "{base}"')
    if pathspec:
        args_parts.append(f'"pathspec": {pathspec}')
    args_str = ", ".join(args_parts)
    if args_str:
        return RouteHint(
            tool_id="git", ui_label="→ git",
            llm_hint=f'Prefer git(command="diff", args={{{args_str}}}) for structured JSON output.',
        )
    return RouteHint(
        tool_id="git", ui_label="→ git",
        llm_hint='Prefer git(command="diff") for structured JSON output.',
    )


_RE_LOG_SHORT_LIMIT = re.compile(r"^-(\d+)$")


def _hint_git_log(rest: list[str]) -> RouteHint | None:
    limit = None
    author = ""
    since = ""
    path = ""
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "-n" and i + 1 < len(rest):
            try:
                limit = int(rest[i + 1])
            except ValueError:
                return None
            i += 2
        elif a == "--" and i + 1 < len(rest):
            path = rest[i + 1]
            break
        elif a.startswith("-"):
            if a.startswith("--author="):
                author = a.split("=", 1)[1]
                i += 1
            elif a.startswith("--since="):
                since = a.split("=", 1)[1]
                i += 1
            else:
                m = _RE_LOG_SHORT_LIMIT.match(a)
                if m:
                    try:
                        limit = int(m.group(1))
                    except ValueError:
                        return None
                    i += 1
                else:
                    return None
        elif not path:
            path = a
            i += 1
        else:
            return None
    args_parts: list[str] = []
    if limit is not None:
        args_parts.append(f'"limit": {limit}')
    if author:
        args_parts.append(f'"author": "{author}"')
    if since:
        args_parts.append(f'"since": "{since}"')
    if path:
        args_parts.append(f'"path": "{path}"')
    args_str = ", ".join(args_parts)
    if args_str:
        return RouteHint(
            tool_id="git", ui_label="→ git",
            llm_hint=f'Prefer git(command="log", args={{{args_str}}}) for structured JSON output.',
        )
    return RouteHint(
        tool_id="git", ui_label="→ git",
        llm_hint='Prefer git(command="log") for structured JSON output.',
    )


def _hint_git_blame(rest: list[str]) -> RouteHint | None:
    if not rest:
        return None
    path = None
    start = None
    end = None
    i = 0
    while i < len(rest):
        if rest[i] == "-L" and i + 1 < len(rest):
            parts = rest[i + 1].split(",", 1)
            if len(parts) != 2:
                return None
            try:
                start = int(parts[0])
                end = int(parts[1])
            except ValueError:
                return None
            i += 2
        elif rest[i].startswith("-"):
            return None
        elif path is None:
            path = rest[i]
            i += 1
        else:
            return None
    if path is None:
        return None
    args_parts = [f'"path": "{path}"']
    if start is not None and end is not None:
        args_parts.append(f'"start": {start}')
        args_parts.append(f'"end": {end}')
    args_str = ", ".join(args_parts)
    return RouteHint(
        tool_id="git", ui_label="→ git",
        llm_hint=f'Prefer git(command="blame", args={{{args_str}}}) for structured JSON output.',
    )


def _hint_git_branch(rest: list[str]) -> RouteHint | None:
    if not rest:
        return RouteHint(
            tool_id="git", ui_label="→ git",
            llm_hint='Prefer git(command="branch_list") for structured JSON output.',
        )
    if rest == ["-a"] or rest == ["--all"]:
        return RouteHint(
            tool_id="git", ui_label="→ git",
            llm_hint='Prefer git(command="branch_list", args={"all": true}) for structured JSON output.',
        )
    if rest[0] in ("-d", "-D", "--delete"):
        force = rest[0] == "-D"
        if len(rest) != 2 or rest[1].startswith("-"):
            return None
        args_parts = [f'"name": "{rest[1]}"']
        if force:
            args_parts.append('"force": true')
        return RouteHint(
            tool_id="git", ui_label="→ git",
            llm_hint=f'Prefer git(command="branch_delete", args={{{", ".join(args_parts)}}}) for permission-scoped git operations.',
        )
    if rest[0].startswith("-"):
        return None
    name = rest[0]
    start_point = ""
    if len(rest) > 2:
        return None
    if len(rest) == 2:
        if rest[1].startswith("-"):
            return None
        start_point = rest[1]
    args_parts = [f'"name": "{name}"']
    if start_point:
        args_parts.append(f'"start_point": "{start_point}"')
    return RouteHint(
        tool_id="git", ui_label="→ git",
        llm_hint=f'Prefer git(command="branch_create", args={{{", ".join(args_parts)}}}) for permission-scoped git operations.',
    )


def _hint_git_remote(rest: list[str]) -> RouteHint | None:
    if rest in (["-v"], ["--verbose"]):
        return RouteHint(
            tool_id="git", ui_label="→ git",
            llm_hint='Prefer git(command="remote_list") for structured JSON output.',
        )
    return None


def _hint_git_add(rest: list[str]) -> RouteHint | None:
    if not rest:
        return None
    paths: list[str] = []
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--":
            paths.extend(rest[i + 1:])
            break
        if a.startswith("-"):
            return None
        paths.append(a)
        i += 1
    if not paths:
        return None
    return RouteHint(
        tool_id="git", ui_label="→ git",
        llm_hint=f'Prefer git(command="add", args={{"paths": {paths}}}) for permission-scoped git operations.',
    )


def _hint_git_commit(stripped: str, rest: list[str]) -> RouteHint | None:
    message = ""
    paths: list[str] = []
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "-m" and i + 1 < len(rest):
            message = rest[i + 1]
            i += 2
        elif a.startswith("-m"):
            message = a[2:]
            i += 1
        elif a.startswith("--message="):
            message = a.split("=", 1)[1]
            i += 1
        elif a == "--" and i + 1 < len(rest):
            paths = rest[i + 1:]
            break
        elif a.startswith("-"):
            return None
        else:
            paths.append(a)
            i += 1
    if not message:
        return None
    if re.search(r'-m\s*"', stripped):
        return None
    if '"' in message:
        return None
    args_parts = [f'"message": "{message}"']
    if paths:
        args_parts.append(f'"paths": {paths}')
    args_str = ", ".join(args_parts)
    return RouteHint(
        tool_id="git", ui_label="→ git",
        llm_hint=f'Prefer git(command="commit", args={{{args_str}}}) for permission-scoped git operations.',
    )


def _hint_git_restore(rest: list[str]) -> RouteHint | None:
    if not rest:
        return None
    staged = False
    paths: list[str] = []
    i = 0
    while i < len(rest):
        if rest[i] == "--staged":
            staged = True
            i += 1
        elif rest[i] == "--":
            paths.extend(rest[i + 1:])
            break
        elif rest[i].startswith("-"):
            return None
        else:
            paths.append(rest[i])
            i += 1
    if not paths:
        return None
    args_parts = [f'"paths": {paths}']
    if staged:
        args_parts.append('"staged": true')
    args_str = ", ".join(args_parts)
    return RouteHint(
        tool_id="git", ui_label="→ git",
        llm_hint=f'Prefer git(command="restore", args={{{args_str}}}) for permission-scoped git operations.',
    )


def _hint_git_show(rest: list[str]) -> RouteHint | None:
    ref = ""
    stat = False
    pathspec: list[str] = []
    i = 0
    while i < len(rest):
        if rest[i] == "--stat":
            stat = True
            i += 1
        elif rest[i] == "--" and i + 1 < len(rest):
            pathspec = rest[i + 1:]
            break
        elif rest[i].startswith("-"):
            return None
        elif not ref:
            ref = rest[i]
            i += 1
        else:
            return None
    args_parts: list[str] = []
    if ref:
        args_parts.append(f'"ref": "{ref}"')
    if stat:
        args_parts.append('"stat": true')
    if pathspec:
        args_parts.append(f'"pathspec": {pathspec}')
    args_str = ", ".join(args_parts)
    if args_str:
        return RouteHint(
            tool_id="git", ui_label="→ git",
            llm_hint=f'Prefer git(command="show", args={{{args_str}}}) for structured JSON output.',
        )
    return RouteHint(
        tool_id="git", ui_label="→ git",
        llm_hint='Prefer git(command="show") for structured JSON output.',
    )


def _hint_git_switch(rest: list[str]) -> RouteHint | None:
    if not rest:
        return None
    create = False
    branch = ""
    start_point = ""
    i = 0
    while i < len(rest):
        if rest[i] == "-c" or rest[i] == "--create":
            create = True
            i += 1
        elif rest[i].startswith("-"):
            return None
        elif not branch:
            branch = rest[i]
            i += 1
        elif not start_point:
            start_point = rest[i]
            i += 1
        else:
            return None
    if not branch:
        return None
    args_parts = [f'"branch": "{branch}"']
    if create:
        args_parts.append('"create": true')
    if start_point:
        args_parts.append(f'"start_point": "{start_point}"')
    args_str = ", ".join(args_parts)
    return RouteHint(
        tool_id="git", ui_label="→ git",
        llm_hint=f'Prefer git(command="switch", args={{{args_str}}}) for permission-scoped git operations.',
    )


def _hint_git_tag(rest: list[str]) -> RouteHint | None:
    if not rest:
        return RouteHint(
            tool_id="git", ui_label="→ git",
            llm_hint='Prefer git(command="tag_list") for structured JSON output.',
        )
    if rest[0] == "-l" or rest[0] == "--list":
        pattern = rest[1] if len(rest) > 1 and not rest[1].startswith("-") else ""
        if pattern:
            return RouteHint(
                tool_id="git", ui_label="→ git",
                llm_hint=f'Prefer git(command="tag_list", args={{"pattern": "{pattern}"}}) for structured JSON output.',
            )
        return RouteHint(
            tool_id="git", ui_label="→ git",
            llm_hint='Prefer git(command="tag_list") for structured JSON output.',
        )
    if rest[0] == "-d" or rest[0] == "--delete":
        name = rest[1] if len(rest) > 1 and not rest[1].startswith("-") else ""
        if name:
            return RouteHint(
                tool_id="git", ui_label="→ git",
                llm_hint=f'Prefer git(command="tag_delete", args={{"name": "{name}"}}) for permission-scoped git operations.',
            )
        return None
    if rest[0].startswith("-"):
        return None
    name = rest[0]
    ref = ""
    message = ""
    force = False
    i = 1
    while i < len(rest):
        if rest[i] == "-a":
            i += 1
        elif rest[i] == "-f":
            force = True
            i += 1
        elif rest[i] == "-m" and i + 1 < len(rest):
            message = rest[i + 1]
            i += 2
        elif rest[i].startswith("-"):
            return None
        elif not ref:
            ref = rest[i]
            i += 1
        else:
            return None
    args_parts = [f'"name": "{name}"']
    if ref:
        args_parts.append(f'"ref": "{ref}"')
    if message:
        args_parts.append(f'"message": "{message}"')
    if force:
        args_parts.append('"force": true')
    return RouteHint(
        tool_id="git", ui_label="→ git",
        llm_hint=f'Prefer git(command="tag_create", args={{{", ".join(args_parts)}}}) for permission-scoped git operations.',
    )


def _hint_git_stash(rest: list[str]) -> RouteHint | None:
    if not rest:
        return RouteHint(
            tool_id="git", ui_label="→ git",
            llm_hint='Prefer git(command="stash_push") or git(command="stash_pop") for structured output.',
        )
    if rest[0] == "push":
        message = ""
        pathspec: list[str] = []
        i = 1
        while i < len(rest):
            if rest[i] == "-m" and i + 1 < len(rest):
                message = rest[i + 1]
                i += 2
            elif rest[i] == "--" and i + 1 < len(rest):
                pathspec = rest[i + 1:]
                break
            elif rest[i].startswith("-"):
                return None
            else:
                i += 1
        args_parts: list[str] = []
        if message:
            args_parts.append(f'"message": "{message}"')
        if pathspec:
            args_parts.append(f'"pathspec": {pathspec}')
        args_str = ", ".join(args_parts)
        if args_str:
            return RouteHint(
                tool_id="git", ui_label="→ git",
                llm_hint=f'Prefer git(command="stash_push", args={{{args_str}}}) for structured output.',
            )
        return RouteHint(
            tool_id="git", ui_label="→ git",
            llm_hint='Prefer git(command="stash_push") for structured output.',
        )
    if rest[0] == "pop" or rest[0] == "apply":
        index = 0
        if len(rest) > 1 and rest[1].startswith("stash@{"):
            try:
                idx_str = rest[1].rstrip("}").split("{")[1]
                index = int(idx_str)
            except (ValueError, IndexError):
                pass
        keep = rest[0] == "apply"
        args_parts = [f'"index": {index}']
        if keep:
            args_parts.append('"keep": true')
        args_str = ", ".join(args_parts)
        return RouteHint(
            tool_id="git", ui_label="→ git",
            llm_hint=f'Prefer git(command="stash_pop", args={{{args_str}}}) for structured output.',
        )
    return None
