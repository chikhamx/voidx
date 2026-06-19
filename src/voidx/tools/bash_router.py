"""Bash command route hint detection — suggest specialized tools over raw bash."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Literal

_HintableTool = Literal["read", "git", "write", "replace", "insert", "glob", "grep"]

_HEREDOC_MAX_CONTENT = 200


@dataclass
class RouteHint:
    tool_id: _HintableTool
    ui_label: str
    llm_hint: str


def try_hint(command: str) -> RouteHint | None:
    """Try to generate a specialized-tool hint for a bash command.

    Catches all exceptions and returns None — hint logic must never break bash.
    """
    try:
        return _try_hint_impl(command)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Shell word splitting
# ---------------------------------------------------------------------------

def _shell_words(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return []


# ---------------------------------------------------------------------------
# Main dispatch
# ---------------------------------------------------------------------------

_RE_AMP = re.compile(r"&&|\s&$")

_UNHINTABLE_GIT_SUBCOMMANDS = frozenset({
    "push", "pull", "merge", "rebase", "stash", "cherry-pick",
    "reset", "checkout", "switch", "fetch", "clone", "init",
    "submodule", "filter-branch", "bisect",
})


def _try_hint_impl(command: str) -> RouteHint | None:
    stripped = command.strip()
    if not stripped:
        return None

    if any(ch in stripped for ch in ("|", ";", "$")):
        return None
    if _RE_AMP.search(stripped):
        return None
    if "`" in stripped or "$(" in stripped:
        return None

    words = _shell_words(stripped)
    if not words:
        return None

    prog = words[0].lower()

    if prog == "git" and len(words) >= 2:
        return _hint_git(stripped, words)
    if prog in ("cat", "head", "tail"):
        if prog == "cat" and "<<" in stripped:
            return _hint_write_heredoc(stripped)
        return _hint_read(words)
    if prog in ("echo", "printf") and ">" in stripped:
        return _hint_write_echo(stripped, words)
    if prog == "find":
        return _hint_find(words)
    if prog in ("grep", "egrep", "fgrep", "rg"):
        return _hint_grep(words)
    if prog == "sed":
        return _hint_sed(words)

    return None


# ---------------------------------------------------------------------------
# Git hints
# ---------------------------------------------------------------------------

def _hint_git(stripped: str, words: list[str]) -> RouteHint | None:
    subcommand = words[1]
    rest = words[2:]
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
        elif rest[i].startswith("-"):
            return None
        else:
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
    pathspec: list[str] = []
    i = 0
    while i < len(rest):
        if rest[i] == "--cached":
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
        else:
            return None
    args_parts: list[str] = []
    if cached:
        args_parts.append('"cached": true')
    if ref:
        args_parts.append(f'"ref": "{ref}"')
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
    return None


def _hint_git_remote(rest: list[str]) -> RouteHint | None:
    if rest == ["-v"]:
        return RouteHint(
            tool_id="git", ui_label="→ git",
            llm_hint='Prefer git(command="remote_list") for structured JSON output.',
        )
    return None


def _hint_git_add(rest: list[str]) -> RouteHint | None:
    if not rest:
        return None
    paths: list[str] = []
    for a in rest:
        if a.startswith("-"):
            return None
        paths.append(a)
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
    # Check if -m argument was double-quoted in the original command.
    # shlex strips quotes, so we inspect the raw string instead.
    # Match both spaced (-m "msg") and compact (-m"msg") forms.
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


# ---------------------------------------------------------------------------
# File read hints
# ---------------------------------------------------------------------------

_RE_HEAD_DIGITS = re.compile(r"^-\d+$")


def _hint_read(words: list[str]) -> RouteHint | None:
    prog = words[0].lower()
    args = words[1:]
    if any(a.startswith("-") and a not in ("-n",) and not _RE_HEAD_DIGITS.match(a) for a in args):
        return None

    if prog == "cat":
        if len(args) != 1:
            return None
        return RouteHint(
            tool_id="read", ui_label="→ read",
            llm_hint=f'Prefer read(file_path="{args[0]}") for line numbers and file tracking.',
        )

    if prog == "head":
        limit = 10
        path = None
        i = 0
        while i < len(args):
            if args[i] == "-n" and i + 1 < len(args):
                try:
                    limit = int(args[i + 1])
                except ValueError:
                    return None
                i += 2
            elif _RE_HEAD_DIGITS.match(args[i]):
                try:
                    limit = int(args[i][1:])
                except ValueError:
                    return None
                i += 1
            elif not args[i].startswith("-"):
                path = args[i]
                i += 1
            else:
                return None
        if path is None:
            return None
        return RouteHint(
            tool_id="read", ui_label="→ read",
            llm_hint=f'Prefer read(file_path="{path}", limit={limit}) for line numbers and file tracking.',
        )

    if prog == "tail":
        return _hint_tail(args)
    return None


def _hint_tail(args: list[str]) -> RouteHint | None:
    if not args:
        return None
    path = None
    offset = None
    i = 0
    while i < len(args):
        if args[i] == "-n" and i + 1 < len(args):
            val = args[i + 1]
            if val.startswith("+"):
                try:
                    offset = int(val[1:])
                except ValueError:
                    return None
            else:
                return None
            i += 2
        elif not args[i].startswith("-"):
            path = args[i]
            i += 1
        else:
            return None
    if path is None or offset is None:
        return None
    return RouteHint(
        tool_id="read", ui_label="→ read",
        llm_hint=f'Prefer read(file_path="{path}", offset={offset}) for line numbers and file tracking.',
    )


# ---------------------------------------------------------------------------
# echo/printf write hints
# ---------------------------------------------------------------------------

def _hint_write_echo(stripped: str, words: list[str]) -> RouteHint | None:
    # Use shlex output to locate the redirect operator — it correctly
    # distinguishes > / >> as redirect tokens from > inside quotes.
    redirect_idx = None
    is_append = False
    for i, w in enumerate(words):
        if w == ">>":
            redirect_idx = i
            is_append = True
            break
        if w == ">":
            redirect_idx = i
            break
    if redirect_idx is None:
        return None

    # Path is the token after the redirect operator
    if redirect_idx + 1 >= len(words):
        return None
    path = words[redirect_idx + 1]

    # Content tokens are between prog (words[0]) and the redirect operator
    prog = words[0].lower()
    content_tokens = words[1:redirect_idx]
    if not content_tokens:
        return None

    # Determine quote type from the raw string
    rest_after_prog = stripped[len(prog):].lstrip()
    if not rest_after_prog:
        return None
    first_char = rest_after_prog[0]

    if first_char == "'":
        # Single-quoted: shlex stripped the quotes, content is the raw value
        content = content_tokens[0]
    elif first_char == '"':
        return None
    else:
        return None

    if '"' in content:
        return None

    if is_append:
        return RouteHint(
            tool_id="insert", ui_label="→ insert",
            llm_hint=f'Prefer insert(file_path="{path}", lineno=-1, new_string="{content}") for file tracking and diff output.',
        )
    return RouteHint(
        tool_id="write", ui_label="→ write",
        llm_hint=f'Prefer write(file_path="{path}", content="{content}") for file tracking and diff output.',
    )


# ---------------------------------------------------------------------------
# heredoc write hints
# ---------------------------------------------------------------------------

_RE_HEREDOC_MARKER = re.compile(r"<<\s*['\"]?(\w+)['\"]?")


def _hint_write_heredoc(stripped: str) -> RouteHint | None:
    is_append = ">>" in stripped
    redirect_op = ">>" if is_append else ">"
    path = None

    if redirect_op in stripped and "<<" in stripped:
        redirect_idx = stripped.index(redirect_op)
        heredoc_idx = stripped.index("<<")
        if redirect_idx < heredoc_idx:
            between = stripped[redirect_idx + len(redirect_op):heredoc_idx].strip()
            path = between.strip("'\"")
        else:
            return None

    if not path:
        return None

    marker_match = _RE_HEREDOC_MARKER.search(stripped)
    if not marker_match:
        return None
    marker = marker_match.group(1)

    marker_start = stripped.find(marker)
    if marker_start == -1:
        return None
    content_start = stripped.find("\n", marker_start)
    if content_start == -1:
        return None
    content_end = stripped.rfind(marker)
    if content_end <= content_start:
        return None
    content = stripped[content_start + 1:content_end].rstrip("\n")

    if len(content) > _HEREDOC_MAX_CONTENT:
        return None
    if '"' in content:
        return None

    if is_append:
        return RouteHint(
            tool_id="insert", ui_label="→ insert",
            llm_hint=f'Prefer insert(file_path="{path}", lineno=-1, new_string="{content}") for file tracking and diff output.',
        )
    return RouteHint(
        tool_id="write", ui_label="→ write",
        llm_hint=f'Prefer write(file_path="{path}", content="{content}") for file tracking and diff output.',
    )


# ---------------------------------------------------------------------------
# find → glob hints
# ---------------------------------------------------------------------------

def _hint_find(words: list[str]) -> RouteHint | None:
    if len(words) < 4:
        return None
    args = words[1:]
    name_pattern = None
    base_dir = "."
    i = 0
    while i < len(args):
        if args[i] == "-name" and i + 1 < len(args):
            name_pattern = args[i + 1]
            i += 2
        elif args[i] == "-type" and i + 1 < len(args):
            if args[i + 1] != "f":
                return None
            i += 2
        elif not args[i].startswith("-") and i == 0:
            base_dir = args[i]
            i += 1
        else:
            return None
    if name_pattern is None:
        return None
    glob_pattern = f"**/{name_pattern}" if base_dir == "." else f"{base_dir}/**/{name_pattern}"
    return RouteHint(
        tool_id="glob", ui_label="→ glob",
        llm_hint=f'Prefer glob(pattern="{glob_pattern}") — skips .git, node_modules, and build dirs automatically.',
    )


# ---------------------------------------------------------------------------
# grep hints
# ---------------------------------------------------------------------------

_RG_TYPE_MAP = {
    "py": "*.py", "js": "*.js", "ts": "*.ts",
    "rs": "*.rs", "go": "*.go", "java": "*.java", "rb": "*.rb",
}


def _hint_grep(words: list[str]) -> RouteHint | None:
    prog = words[0].lower()
    args = words[1:]
    include = None
    pattern = None
    path = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-r", "-R"):
            i += 1
        elif a == "--include" and i + 1 < len(args):
            include = args[i + 1]
            i += 2
        elif a.startswith("--include="):
            include = a.split("=", 1)[1]
            i += 1
        elif a == "-t" and i + 1 < len(args) and prog == "rg":
            type_name = args[i + 1]
            include = _RG_TYPE_MAP.get(type_name)
            if include is None:
                return None
            i += 2
        elif a.startswith("-") and a not in ("-e",):
            return None
        elif pattern is None:
            pattern = a
            i += 1
        elif path is None:
            path = a
            i += 1
        else:
            return None
    if pattern is None:
        return None
    if prog == "fgrep":
        pattern = re.escape(pattern)
    parts = [f'pattern="{pattern}"']
    if path:
        parts.append(f'path="{path}"')
    if include:
        parts.append(f'include="{include}"')
    return RouteHint(
        tool_id="grep", ui_label="→ grep",
        llm_hint=f'Prefer grep({", ".join(parts)}) — skips .git, node_modules, and binary files automatically.',
    )


# ---------------------------------------------------------------------------
# sed hints
# ---------------------------------------------------------------------------

_SED_SIMPLE = re.compile(r"^(\d+)s/([^/]*)/([^/]*)/?$")
_SED_GLOBAL = re.compile(r"^s/([^/]*)/([^/]*)/g?$")
_SED_RANGE_DELETE = re.compile(r"^(\d+),(\d+)d$")
_SED_PATTERN_DELETE = re.compile(r"^/(.+)/d$")


def _hint_sed(words: list[str]) -> RouteHint | None:
    if len(words) < 3:
        return None
    args = words[1:]
    i = 0
    if args[i] == "-i":
        i += 1
        if i < len(args) and args[i] == "":
            i += 1
    elif args[i].startswith("-i"):
        i += 1
    else:
        return None
    if i >= len(args):
        return None
    script = args[i]; i += 1
    path = args[i] if i < len(args) else None; i += 1
    if i != len(args) or script is None or path is None:
        return None

    m = _SED_SIMPLE.match(script)
    if m:
        line_no, old_text, new_text = int(m.group(1)), m.group(2), m.group(3)
        if "&" not in new_text and r"\1" not in new_text:
            return RouteHint(
                tool_id="replace", ui_label="→ replace",
                llm_hint=f'Prefer replace(file_path="{path}", start_no={line_no}, end_no={line_no}, prefix="{old_text}", suffix="{old_text}", new_string="{new_text}") — prefix/suffix are line content anchors for locating the edit, new_string is the replacement. Enables staleness checking and diff output.',
            )

    m = _SED_GLOBAL.match(script)
    if m:
        old_text, new_text = m.group(1), m.group(2)
        if "&" not in new_text and r"\1" not in new_text:
            return RouteHint(
                tool_id="replace", ui_label="→ replace",
                llm_hint=f'For global substitution: first read {path} to locate lines, then use replace(file_path, start_no, end_no, prefix="{old_text}", suffix="{old_text}", new_string="{new_text}") — prefix/suffix are line content anchors for locating the edit.',
            )

    m = _SED_RANGE_DELETE.match(script)
    if m:
        start, end = int(m.group(1)), int(m.group(2))
        return RouteHint(
            tool_id="replace", ui_label="→ replace",
            llm_hint=f'For line range deletion: first read {path} to see lines {start}-{end}, then use replace(file_path, start_no={start}, end_no={end}, prefix=<first_line_content>, suffix=<last_line_content>, new_string="").',
        )

    m = _SED_PATTERN_DELETE.match(script)
    if m:
        pat = m.group(1)
        return RouteHint(
            tool_id="replace", ui_label="→ replace",
            llm_hint=f'For pattern-based deletion: first grep "{pat}" {path} to locate lines, then use replace(..., new_string="").',
        )

    return None
