"""Route simple in-place sed edits through managed file tools."""

from __future__ import annotations

from dataclasses import dataclass

from voidx.tooling.domain.context import ToolExecutionContext as ToolContext
from voidx.tooling.domain.result import ToolResult
from voidx.tooling.builtin.shell.bash.core import (
    _RE_AMP,
    _has_shell_expansion,
    _has_unquoted_pathname_expansion,
    shell_words,
    _strip_cd_prefix,
)
from voidx.tooling.builtin.shell.bash.hint.search import SED_LINE_DELETE, SED_RANGE_DELETE, sed_split
from voidx.tooling.builtin.shell.common import RouteHint, build_hint_result


_SED_REGEX_META = set(".^$*+?[](){}|")


@dataclass(frozen=True)
class _SedCommand:
    script: str
    path: str
    routable: bool = True


@dataclass(frozen=True)
class _SedSubstitution:
    line_no: int
    old_text: str
    new_text: str
    global_replace: bool


async def maybe_route_sed_edit(command: str, ctx: ToolContext, source: str) -> ToolResult | None:
    parsed = _parse_simple_sed_inplace(command)
    if parsed is None:
        return None

    hint = _hint_for_sed(parsed)
    if not parsed.routable:
        return build_hint_result(command, hint, "Bash")
    tool_invoker = ctx.tool_invoker
    if tool_invoker is None or tool_invoker.get("read") is None or tool_invoker.get("replace") is None:
        return build_hint_result(command, hint, "Bash")

    substitution = _parse_safe_line_substitution(parsed.script)
    if substitution is not None:
        return await _route_sed_substitution(command, parsed, substitution, ctx, source)

    line_delete = _parse_line_delete(parsed.script)
    if line_delete is not None:
        return await _route_sed_delete(command, parsed.path, line_delete, line_delete, ctx, source)

    range_delete = _parse_range_delete(parsed.script)
    if range_delete is not None:
        start, end = range_delete
        return await _route_sed_delete(command, parsed.path, start, end, ctx, source)

    return build_hint_result(command, hint, "Bash")


def _parse_simple_sed_inplace(command: str) -> _SedCommand | None:
    stripped = command.strip()
    if not stripped or _strip_cd_prefix(stripped) != stripped:
        return None
    if ";" in stripped or _has_shell_expansion(stripped) or _has_unquoted_pathname_expansion(stripped):
        return None
    if _RE_AMP.search(stripped):
        return None

    words = shell_words(stripped)
    if len(words) < 4 or words[0].lower() != "sed":
        return None
    if any(word in {"|", "|&"} for word in words):
        return None

    args = words[1:]
    script: str | None = None
    path: str | None = None
    inplace = False
    unsupported = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "-i":
            inplace = True
            i += 1
            if i < len(args) and args[i] == "":
                i += 1
        elif arg.startswith("-i"):
            inplace = True
            unsupported = True
            i += 1
        elif arg == "-e":
            if i + 1 >= len(args) or script is not None:
                unsupported = True
                break
            script = args[i + 1]
            i += 2
        elif arg.startswith("-"):
            unsupported = True
            i += 1
        elif script is None:
            script = arg
            i += 1
        elif path is None:
            path = arg
            i += 1
        else:
            unsupported = True
            i += 1

    if not inplace:
        return None
    if unsupported or script is None or path is None:
        return _SedCommand(script=script or "", path=path or "", routable=False)
    return _SedCommand(script=script, path=path)


def _parse_safe_line_substitution(script: str) -> _SedSubstitution | None:
    if "\\" in script:
        return None
    parsed = sed_split(script)
    if parsed is None:
        return None
    line_prefix, old_text, new_text, flags = parsed
    if not line_prefix or not old_text:
        return None
    if any(flag != "g" for flag in flags):
        return None
    if any(ch in _SED_REGEX_META for ch in old_text):
        return None
    if "\\" in old_text or "\\" in new_text or "&" in new_text:
        return None
    try:
        line_no = int(line_prefix)
    except ValueError:
        return None
    if line_no <= 0:
        return None
    return _SedSubstitution(
        line_no=line_no,
        old_text=old_text,
        new_text=new_text,
        global_replace="g" in flags,
    )


def _parse_line_delete(script: str) -> int | None:
    match = SED_LINE_DELETE.match(script)
    if not match:
        return None
    line_no = int(match.group(1))
    return line_no if line_no > 0 else None


def _parse_range_delete(script: str) -> tuple[int, int] | None:
    match = SED_RANGE_DELETE.match(script)
    if not match:
        return None
    start = int(match.group(1))
    end = int(match.group(2))
    if start <= 0 or end < start:
        return None
    return start, end


async def _route_sed_substitution(
    command: str,
    parsed: _SedCommand,
    substitution: _SedSubstitution,
    ctx: ToolContext,
    source: str,
) -> ToolResult:
    line = await _read_managed_line(parsed.path, substitution.line_no, ctx)
    if isinstance(line, ToolResult):
        return line
    if substitution.old_text not in line:
        return _routed_no_change(command, parsed.path, source)
    if substitution.global_replace:
        new_line = line.replace(substitution.old_text, substitution.new_text)
    else:
        new_line = line.replace(substitution.old_text, substitution.new_text, 1)
    tool_args = {
        "file_path": parsed.path,
        "bounds": [{"line_no": substitution.line_no, "anchor": substitution.old_text}],
        "new_string": new_line,
    }
    result = await ctx.tool_invoker.execute_tool("replace", tool_args, ctx)
    return _mark_routed(result, command, tool_args, source)


async def _route_sed_delete(
    command: str,
    path: str,
    start: int,
    end: int,
    ctx: ToolContext,
    source: str,
) -> ToolResult:
    lines = await _read_managed_lines(path, start, end, ctx)
    if isinstance(lines, ToolResult):
        return lines
    if not lines:
        return _routed_no_change(command, path, source)
    if len(lines) == 1:
        bounds = [{"line_no": start, "anchor": lines[0]}]
    elif lines[0] == "" or lines[-1] == "":
        return build_hint_result(command, _hint_for_sed(_SedCommand(script=f"{start},{end}d", path=path)), "Bash")
    else:
        bounds = [
            {"line_no": start, "anchor": lines[0]},
            {"line_no": end, "anchor": lines[-1]},
        ]
    tool_args = {
        "file_path": path,
        "bounds": bounds,
        "new_string": "",
    }
    result = await ctx.tool_invoker.execute_tool("replace", tool_args, ctx)
    return _mark_routed(result, command, tool_args, source)


async def _read_managed_line(path: str, line_no: int, ctx: ToolContext) -> str | ToolResult:
    lines = await _read_managed_lines(path, line_no, line_no, ctx)
    if isinstance(lines, ToolResult):
        return lines
    if not lines:
        return ToolResult(output=f"Line {line_no} not found in {path}.", metadata={"error": True})
    return lines[0]


async def _read_managed_lines(path: str, start: int, end: int, ctx: ToolContext) -> list[str] | ToolResult:
    read_args = {"file_path": path, "offset": start, "limit": end - start + 1}
    result = await ctx.tool_invoker.execute_tool("read", read_args, ctx)
    if result.metadata.get("error"):
        return result
    lines_by_no: dict[int, str] = {}
    for raw_line in result.output.splitlines():
        prefix, separator, text = raw_line.partition("\t")
        if separator and prefix.isdigit():
            lines_by_no[int(prefix)] = text
    expected = list(range(start, end + 1))
    if any(line_no not in lines_by_no for line_no in expected):
        return ToolResult(
            output=f"Could not resolve sed target lines {start}-{end} in {path}. Read the range and use replace directly.",
            metadata={"error": True},
        )
    return [lines_by_no[line_no] for line_no in expected]


def _routed_no_change(command: str, path: str, source: str) -> ToolResult:
    return ToolResult(
        title="No changes",
        output=f"No changes: {path}",
        summary="No changes",
        metadata={
            "file": path,
            "operations": 0,
            "tool": "replace",
            "routed_from": source.lower(),
            "routed_command": command,
            "routed_tool_args": None,
        },
    )


def _mark_routed(result: ToolResult, command: str, tool_args: dict, source: str) -> ToolResult:
    result.metadata = {
        **result.metadata,
        "tool": "replace",
        "routed_from": source.lower(),
        "routed_command": command,
        "routed_tool_args": tool_args,
    }
    return result


def _hint_for_sed(parsed: _SedCommand) -> RouteHint:
    script = parsed.script
    path = parsed.path or "<path>"
    substitution = _parse_safe_line_substitution(script)
    if substitution is not None:
        return RouteHint(
            tool_id="replace",
            ui_label="→ replace",
            llm_hint=(
                f'Prefer read(file_path="{path}", offset={substitution.line_no}, limit=1), '
                f'then replace(file_path="{path}", bounds=[{{"line_no": {substitution.line_no}, '
                f'"anchor": "{substitution.old_text}"}}], new_string="<full replacement line>").'
            ),
        )
    line_delete = _parse_line_delete(script)
    if line_delete is not None:
        return RouteHint(
            tool_id="replace",
            ui_label="→ replace",
            llm_hint=(
                f'Prefer read(file_path="{path}", offset={line_delete}, limit=1), '
                f'then replace(file_path="{path}", bounds=[{{"line_no": {line_delete}, '
                '"anchor": "<line content>"}}], new_string="").'
            ),
        )
    range_delete = _parse_range_delete(script)
    if range_delete is not None:
        start, end = range_delete
        return RouteHint(
            tool_id="replace",
            ui_label="→ replace",
            llm_hint=(
                f'Prefer read(file_path="{path}", offset={start}, limit={end - start + 1}), '
                f'then replace(file_path="{path}", bounds=[start/end anchors], new_string="").'
            ),
        )
    return RouteHint(
        tool_id="replace",
        ui_label="→ replace",
        llm_hint=f'For sed in-place edits, locate the affected lines in {path}, then use replace so file tracking and diffs stay managed.',
    )
