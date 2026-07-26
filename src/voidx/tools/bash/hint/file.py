"""File operation route hints — read, tail, write (echo/heredoc), find → glob."""

from __future__ import annotations

import re

from voidx.tools.bash.core import RouteHint, _HEREDOC_MAX_CONTENT, _shell_words

_RE_HEAD_DIGITS = re.compile(r"^-\d+$")


def _hint_read(words: list[str]) -> RouteHint | None:
    prog = words[0].lower()
    args = words[1:]
    if any(a.startswith("-") and a not in ("-n",) and not _RE_HEAD_DIGITS.match(a) for a in args):
        return None

    if prog == "cat":
        if len(args) != 1 or args[0].startswith("-"):
            return None
        return RouteHint(
            tool_id="read", ui_label="→ read",
            llm_hint=f'Prefer read(file_path="{args[0]}") for line numbers and file tracking.',
            tool_args={"file_path": args[0]},
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
                if path is not None:
                    return None
                path = args[i]
                i += 1
            else:
                return None
        if path is None or limit <= 0:
            return None
        return RouteHint(
            tool_id="read", ui_label="→ read",
            llm_hint=f'Prefer read(file_path="{path}", limit={limit}) for line numbers and file tracking.',
            tool_args={"file_path": path, "limit": limit},
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
            if path is not None:
                return None
            path = args[i]
            i += 1
        else:
            return None
    if path is None or offset is None or offset <= 0:
        return None
    return RouteHint(
        tool_id="read", ui_label="→ read",
        llm_hint=f'Prefer read(file_path="{path}", offset={offset}) for line numbers and file tracking.',
        tool_args={"file_path": path, "offset": offset},
    )


def _hint_write_echo(stripped: str, words: list[str]) -> RouteHint | None:
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

    if redirect_idx + 1 >= len(words):
        return None
    path = words[redirect_idx + 1]

    prog = words[0].lower()
    content_tokens = words[1:redirect_idx]
    if not content_tokens:
        return None

    rest_after_prog = stripped[len(prog):].lstrip()
    if not rest_after_prog:
        return None
    first_char = rest_after_prog[0]

    if first_char == "'":
        content = content_tokens[0]
    elif first_char == '"':
        return None
    else:
        return None

    if '"' in content:
        return None

    if is_append:
        return RouteHint(
            tool_id="write", ui_label="→ write",
            llm_hint=f'Prefer write(file_path="{path}", op="append", new_string="{content}") for file tracking and diff output.',
        )
    return RouteHint(
        tool_id="manage", ui_label="→ manage",
        llm_hint=f'Prefer manage(op="create", paths="{path}") then write(file_path="{path}", op="append", new_string="{content}") for file tracking and diff output.',
    )


_RE_HEREDOC_MARKER = re.compile(r"<<\s*['\"]?(\w+)['\"]?")


def _hint_write_heredoc(stripped: str) -> RouteHint | None:
    words = _shell_words(stripped)
    path = None
    is_append = False
    for i, word in enumerate(words):
        if word in (">", ">>") and i + 1 < len(words):
            is_append = word == ">>"
            path = words[i + 1]
            break

    if path is None:
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
            tool_id="write", ui_label="→ write",
            llm_hint=f'Prefer write(file_path="{path}", op="append", new_string="{content}") for file tracking and diff output.',
        )
    return RouteHint(
        tool_id="manage", ui_label="→ manage",
        llm_hint=f'Prefer manage(op="create", paths="{path}") then write(file_path="{path}", op="append", new_string="{content}") for file tracking and diff output.',
    )


def _hint_find(words: list[str]) -> RouteHint | None:
    if len(words) < 4:
        return None
    args = words[1:]
    name_pattern = None
    case = "sensitive"
    base_dir = "."
    file_only = False
    i = 0
    while i < len(args):
        if args[i] == "-name" and i + 1 < len(args):
            name_pattern = args[i + 1]
            i += 2
        elif args[i] == "-iname" and i + 1 < len(args):
            name_pattern = args[i + 1]
            case = "insensitive"
            i += 2
        elif args[i] == "-type" and i + 1 < len(args):
            if args[i + 1] != "f":
                return None
            file_only = True
            i += 2
        elif args[i].startswith("-"):
            return None
        elif i == 0:
            base_dir = args[i]
            i += 1
        else:
            return None
    if name_pattern is None or not file_only:
        return None
    normalized_base = base_dir.removeprefix("./").rstrip("/") or "."
    if base_dir.startswith("/") or ".." in normalized_base.split("/") or "/" in name_pattern or "\\" in name_pattern:
        return None
    if any(ch in name_pattern for ch in "?[{"):
        return None
    query = None
    extensions = None
    if name_pattern.startswith("*.") and name_pattern.count("*") == 1:
        ext = name_pattern[2:]
        if not ext or any(ch in ext for ch in "*?[{"):
            return None
        extensions = [ext]
    elif name_pattern.startswith("*") and name_pattern.endswith("*"):
        query = name_pattern[1:-1]
    elif name_pattern.startswith("*"):
        query = name_pattern[1:]
        if not query:
            return None
    elif name_pattern.endswith("*"):
        query = name_pattern[:-1]
        if not query:
            return None
    else:
        return None
    tool_args: dict = {"path": normalized_base, "case": case}
    if query:
        tool_args["query"] = query
    if extensions:
        tool_args["extensions"] = extensions
    return RouteHint(
        tool_id="find", ui_label="→ find",
        llm_hint=f'Prefer find({tool_args}) — stable filename results.',
        tool_args=tool_args,
    )
