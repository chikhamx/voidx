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
        tool_id="file", ui_label="→ file",
        llm_hint=f'Prefer file(file_path="{path}", op="create") then line(file_path="{path}", op="append", new_string="{content}") for file tracking and diff output.',
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
        tool_id="file", ui_label="→ file",
        llm_hint=f'Prefer file(file_path="{path}", op="create") then line(file_path="{path}", op="append", new_string="{content}") for file tracking and diff output.',
    )


def _hint_find(words: list[str]) -> RouteHint | None:
    if len(words) < 4:
        return None
    args = words[1:]
    name_pattern = None
    ignore_case = False
    max_depth = None
    base_dir = "."
    i = 0
    while i < len(args):
        if args[i] == "-name" and i + 1 < len(args):
            name_pattern = args[i + 1]
            i += 2
        elif args[i] == "-iname" and i + 1 < len(args):
            name_pattern = args[i + 1]
            ignore_case = True
            i += 2
        elif args[i] == "-maxdepth" and i + 1 < len(args):
            try:
                max_depth = int(args[i + 1])
            except ValueError:
                return None
            if max_depth < 0:
                return None
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
    parts = [f'pattern="{glob_pattern}"']
    if ignore_case:
        parts.append("ignore_case=True")
    if max_depth is not None:
        parts.append(f"max_depth={max_depth}")
    return RouteHint(
        tool_id="glob", ui_label="→ glob",
        llm_hint=f'Prefer glob({", ".join(parts)}) — skips .git, node_modules, and build dirs automatically.',
    )
