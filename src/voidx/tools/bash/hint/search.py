"""Search route hints — grep → grep tool, sed → replace tool."""

from __future__ import annotations

import re

from voidx.tools.bash.core import RouteHint

_RG_TYPE_MAP = {
    "py": "*.py", "js": "*.js", "ts": "*.ts",
    "rs": "*.rs", "go": "*.go", "java": "*.java", "rb": "*.rb",
}


def _regex_is_simple_python_compatible(pattern: str) -> bool:
    try:
        re.compile(pattern)
    except re.error:
        return False
    return "[[:" not in pattern


def _grep_bre_is_python_compatible(pattern: str) -> bool:
    if "[[:" in pattern:
        return False
    escaped = False
    for ch in pattern:
        if escaped:
            if ch.isalnum() or ch in "+?(){}|":
                return False
            escaped = False
            continue
        if ch == "\\":
            escaped = True
        elif ch in "+?(){}|":
            return False
    return True


def _parse_grep_short_flags(flags: str) -> dict[str, bool] | None:
    parsed = {"recursive": False, "line_number": False, "ignore_case": False, "whole_word": False}
    for flag in flags:
        if flag in ("r", "R"):
            parsed["recursive"] = True
        elif flag == "n":
            parsed["line_number"] = True
        elif flag == "i":
            parsed["ignore_case"] = True
        elif flag == "w":
            parsed["whole_word"] = True
        else:
            return None
    return parsed


def _hint_grep(words: list[str]) -> RouteHint | None:
    prog = words[0].lower()
    args = words[1:]
    pattern = None
    path = None
    extensions = None
    match_mode = "text" if prog == "fgrep" else "regex"
    case = "sensitive"
    context = 0
    before = after = 0
    recursive = prog == "rg"
    fixed = prog == "fgrep"
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-r", "-R"):
            if prog == "rg":
                return None
            recursive = True
            i += 1
        elif a in ("-n", "--line-number"):
            i += 1
        elif a in ("-F", "--fixed-strings"):
            fixed = True
            match_mode = "text"
            i += 1
        elif a in ("-i", "--ignore-case"):
            case = "insensitive"
            i += 1
        elif a in ("-S", "--smart-case") and prog == "rg":
            case = "auto"
            i += 1
        elif a in ("-w", "--word-regexp"):
            match_mode = "word"
            i += 1
        elif a in ("-C", "--context") and i + 1 < len(args):
            try:
                context = int(args[i + 1])
            except ValueError:
                return None
            i += 2
        elif a in ("-A", "--after-context") and i + 1 < len(args):
            try:
                after = int(args[i + 1])
            except ValueError:
                return None
            i += 2
        elif a in ("-B", "--before-context") and i + 1 < len(args):
            try:
                before = int(args[i + 1])
            except ValueError:
                return None
            i += 2
        elif a == "-t" and prog == "rg" and i + 1 < len(args):
            type_name = args[i + 1]
            ext = _RG_TYPE_MAP.get(type_name)
            if ext is None:
                return None
            extensions = [ext.removeprefix("*.")]
            i += 2
        elif a.startswith("-"):
            return None
        elif pattern is None:
            pattern = a
            i += 1
        elif path is None:
            path = a
            i += 1
        else:
            return None
    if pattern is None or (path is None and not recursive) or context < 0 or before < 0 or after < 0:
        return None
    if context and (before or after) or before != after:
        return None
    if fixed:
        match_mode = "text"
    elif prog == "grep" and not _grep_bre_is_python_compatible(pattern):
        return None
    elif prog in ("egrep", "rg") and not _regex_is_simple_python_compatible(pattern):
        return None
    tool_args: dict = {"query": pattern, "match": match_mode, "case": case}
    if path:
        tool_args["path"] = path
    if extensions:
        tool_args["extensions"] = extensions
    effective_context = context or before
    if effective_context:
        tool_args["context"] = effective_context
    return RouteHint(
        tool_id="search", ui_label="→ search",
        llm_hint=f'Prefer search({tool_args}) — structured matches with context.',
        tool_args=tool_args,
    )


# ---------------------------------------------------------------------------
# sed hints
# ---------------------------------------------------------------------------

_SED_RANGE_DELETE = re.compile(r"^(\d+),(\d+)d$")
_SED_LINE_DELETE = re.compile(r"^(\d+)d$")
_SED_PATTERN_DELETE = re.compile(r"^/(.+)/d$")


def _sed_split(script: str) -> tuple[str, str, str, str] | None:
    """Parse a sed substitution script into (line_prefix, old, new, flags).

    Supports any delimiter (``s/old/new/``, ``s|old|new|``, ``s#old#new#``)
    and escaped delimiters within old/new (e.g. ``\\/``).
    Returns None if the script is not a valid substitution.
    """
    m = re.match(r"^(\d*)s", script)
    if not m:
        return None
    line_prefix = m.group(1)
    rest = script[m.end():]
    if not rest:
        return None
    delim = rest[0]
    if delim.isalnum() or delim == "\\":
        return None
    parts: list[str] = []
    current: list[str] = []
    i = 1
    while i < len(rest):
        ch = rest[i]
        if ch == "\\" and i + 1 < len(rest):
            current.append(rest[i + 1])
            i += 2
            continue
        if ch == delim:
            parts.append("".join(current))
            current = []
            i += 1
            if len(parts) == 3:
                flags = rest[i:]
                return line_prefix, parts[0], parts[1], flags
            continue
        current.append(ch)
        i += 1
    if len(parts) == 2:
        flags = "".join(current)
        return line_prefix, parts[0], parts[1], flags
    if len(parts) == 1 and current:
        return line_prefix, parts[0], "".join(current), ""
    return None


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

    parsed = _sed_split(script)
    if parsed:
        line_prefix, old_text, new_text, flags = parsed
        is_global = "g" in flags
        if "&" not in new_text and r"\1" not in new_text:
            if line_prefix:
                line_no = int(line_prefix)
                return RouteHint(
                    tool_id="replace", ui_label="→ replace",
                    llm_hint=f'Prefer replace(file_path="{path}", start_no={line_no}, end_no={line_no}, start_anchor="{old_text}", end_anchor="{old_text}", new_string="{new_text}").',
                )
            if is_global:
                return RouteHint(
                    tool_id="replace", ui_label="→ replace",
                    llm_hint=f'For global substitution: first read {path} to locate lines, then use replace(file_path, start_no, end_no, start_anchor="{old_text}", end_anchor="{old_text}", new_string="{new_text}").',
                )
            return RouteHint(
                tool_id="replace", ui_label="→ replace",
                llm_hint=f'Prefer replace(file_path="{path}", start_no, end_no, start_anchor="{old_text}", end_anchor="{old_text}", new_string="{new_text}").',
            )

    m = _SED_RANGE_DELETE.match(script)
    if m:
        start, end = int(m.group(1)), int(m.group(2))
        return RouteHint(
            tool_id="replace", ui_label="→ replace",
            llm_hint=f'For line range deletion: first read {path} to see lines {start}-{end}, then use replace(file_path="{path}", start_no={start}, end_no={end}, start_anchor="...", end_anchor="...", new_string="").',
        )

    m = _SED_LINE_DELETE.match(script)
    if m:
        line_no = int(m.group(1))
        return RouteHint(
            tool_id="replace", ui_label="→ replace",
            llm_hint=f'For single line deletion: first read {path} to see line {line_no}, then use replace(file_path="{path}", start_no={line_no}, end_no={line_no}, start_anchor="...", end_anchor="...", new_string="").',
        )
    m = _SED_PATTERN_DELETE.match(script)
    if m:
        pat = m.group(1)
        return RouteHint(
            tool_id="replace", ui_label="→ replace",
            llm_hint=f'For pattern-based deletion: first grep "{pat}" {path} to locate matching lines, then use replace(file_path="{path}", start_no, end_no, start_anchor, end_anchor, new_string="") with the matched line numbers and content.',
        )

    return None
