from __future__ import annotations

from dataclasses import asdict
from typing import NamedTuple

from pydantic import BaseModel, Field, model_validator

from voidx.diffing import make_file_diff, make_structured_diff, render_numbered_diff
from voidx.logging.tool_log import log_tool_event
from voidx.tools.base import BaseTool, ToolContext, ToolResult, model_to_json_schema, resolve_safe
from voidx.tools.file_state import (
    LineDriftMap,
    check_read_coverage,
    check_staleness,
    clear_read_coverage,
    file_fingerprint,
    get_line_drift_maps,
    record_mtime,
    remap_read_coverage_from_file_diff,
    save_file_version,
)

from .edit_resolve import (
    _find_text_segment,
    _result_trailing_newline,
    _validate_resolved_edits,
    remap_line_range,
)
from .read import _join_display_lines, _split_display_lines, _split_edit_lines
from .types import DisplayLines, ResolvedEdit



class ReplaceBound(BaseModel):
    line_no: int = Field(
        ge=1,
        description="Line number (1-based) from the latest read output.",
    )
    anchor: str = Field(
        description=(
            "Substring expected on this boundary line. For single-line replace, "
            "empty anchor skips anchor validation and uses line_no directly. "
            "For multi-line replace, anchor must be non-empty. Does not span lines — "
            "a multi-line anchor uses only its first non-empty line."
        ),
    )


class FileReplaceInput(BaseModel):
    file_path: str = Field(description="Absolute or relative path to the file")
    bounds: list[ReplaceBound] = Field(
        min_length=1,
        max_length=2,
        description=(
            "Replacement boundary lines. Provide one bound for single-line replace, "
            "or two unordered bounds for multi-line replace. In multi-line replace, "
            "both anchors must be non-empty; the smaller line_no is used as the start "
            "boundary and the larger line_no is used as the end boundary."
        ),
    )
    new_string: str = Field(
        description=(
            "Content that replaces the selected whole line or line range. Empty string "
            "deletes the selected lines. The replacement string's trailing newline is "
            "ignored for line splitting; the original file's trailing-newline state is "
            "preserved unless the file becomes empty. If the first or last line of "
            "new_string exactly matches the line immediately before or after the replaced "
            "range, that adjacent line is also consumed."
        ),
    )

    @model_validator(mode="after")
    def _validate_bounds(self) -> "FileReplaceInput":
        if len(self.bounds) == 2:
            if self.bounds[0].line_no == self.bounds[1].line_no:
                raise ValueError("two-bound replace requires different line_no values; use one bound for single-line replace")
            if self.bounds[0].anchor == "" or self.bounds[1].anchor == "":
                raise ValueError("multi-line replace requires non-empty anchors for both boundary lines")
        return self

    def _ordered_bounds(self) -> tuple[ReplaceBound, ReplaceBound]:
        if len(self.bounds) == 1:
            return self.bounds[0], self.bounds[0]
        first, second = sorted(self.bounds, key=lambda bound: bound.line_no)
        return first, second

    @property
    def resolved_start_no(self) -> int:
        return self._ordered_bounds()[0].line_no

    @property
    def resolved_end_no(self) -> int:
        return self._ordered_bounds()[1].line_no

    @property
    def resolved_start_anchor(self) -> str:
        return self._ordered_bounds()[0].anchor

    @property
    def resolved_end_anchor(self) -> str:
        return self._ordered_bounds()[1].anchor


_REQUIRED_REPLACE_FIELDS = "file_path, bounds, new_string"


def _extract_field_from_message(message: str) -> str | None:
    candidates = [name for name in _REQUIRED_REPLACE_FIELDS.split(", ") if name in message]
    if not candidates:
        return None
    return min(candidates, key=lambda name: message.index(name))


def _clean_message(message: str) -> str:
    prefix = "Value error, "
    if message.startswith(prefix):
        return message[len(prefix):]
    return message


def _format_replace_validation_error(exc: Exception) -> str:
    errors = getattr(exc, "errors", lambda: [])()
    if errors:
        first = next((error for error in errors if error.get("type") == "missing"), errors[0])
        loc = first.get("loc") or []
        field = str(loc[0]) if loc else None
        error_type = first.get("type", "")
        if error_type == "missing":
            return (
                f"Invalid arguments: field '{field}' is required. "
                f"Required fields: {_REQUIRED_REPLACE_FIELDS}."
            )
        message = _clean_message(first.get("msg", "is invalid"))
        if field is None:
            ctx_error = str(first.get("ctx", {}).get("error", "") or "")
            field = _extract_field_from_message(ctx_error or message)
        if field is not None:
            return (
                f"Invalid arguments: field '{field}' {message}. "
                f"Required fields: {_REQUIRED_REPLACE_FIELDS}."
            )
        return (
            f"Invalid arguments: {message}. "
            f"Required fields: {_REQUIRED_REPLACE_FIELDS}."
        )
    return f"Invalid arguments. Required fields: {_REQUIRED_REPLACE_FIELDS}."


_TRUNCATE = 200


def _truncate(text: str, limit: int = _TRUNCATE) -> str:
    text = text.replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit] + "…"


def _log_replace_failure(
    *,
    tool_name: str,
    file_path: str,
    reason: str,
    ctx: ToolContext | None = None,
    start_no: int | None = None,
    end_no: int | None = None,
    start_anchor: str | None = None,
    end_anchor: str | None = None,
    new_string: str | None = None,
    lines: list[str] | None = None,
) -> None:
    parts: list[str] = [f"file_path={file_path}", f"reason={reason}"]
    if start_no is not None:
        parts.append(f"start_no={start_no}")
    if end_no is not None:
        parts.append(f"end_no={end_no}")
    if start_anchor is not None:
        parts.append(f"start_anchor={_truncate(start_anchor)!r}")
    if end_anchor is not None:
        parts.append(f"end_anchor={_truncate(end_anchor)!r}")
    if new_string is not None:
        parts.append(f"new_string={_truncate(new_string)!r}")
    if lines is not None:
        idx_start = (start_no or 1) - 1
        idx_end = (end_no or start_no or 1) - 1
        if 0 <= idx_start < len(lines):
            parts.append(f"actual_start_line={_truncate(lines[idx_start])!r}")
        if idx_end != idx_start and 0 <= idx_end < len(lines):
            parts.append(f"actual_end_line={_truncate(lines[idx_end])!r}")
        total = len(lines)
        parts.append(f"total_lines={total}")
    log_tool_event(
        "replace_failed",
        tool_name=tool_name,
        message=", ".join(parts),
        session_id=ctx.session_id if ctx is not None else None,
    )


class FileReplaceTool(BaseTool):
    id = "replace"
    description = (
        "Replace whole lines in a file. "
        "Provide one bound for single-line replace or two unordered bounds for multi-line replace. "
        "Read the target lines first. "
        "Single-line replace may use an empty anchor to trust line_no directly. "
        "Multi-line replace requires non-empty anchors on both boundary lines; "
        "the smaller line_no is used as the start boundary and the larger line_no is used as the end boundary. "
        "Anchors are searched near the given line numbers in case the file changed since the last read. "
        "If the file does not exist, it is created with new_string as its content."
    )

    def parameters_schema(self) -> dict:
        schema = model_to_json_schema(FileReplaceInput)
        properties = schema.get("properties", {})
        visible_fields = ("file_path", "bounds", "new_string")
        schema["properties"] = {name: properties[name] for name in visible_fields if name in properties}
        schema["required"] = list(visible_fields)
        return schema

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            inp = FileReplaceInput.model_validate(args)
        except Exception as exc:
            output = _format_replace_validation_error(exc)
            _log_replace_failure(
                tool_name=self.id,
                file_path=str(args.get("file_path", "?")),
                reason=output,
                ctx=ctx,
            )
            return ToolResult(output=output, metadata={"error": True})
        return await _execute_text_replace(
            ctx,
            file_path=inp.file_path,
            start_no=inp.resolved_start_no,
            end_no=inp.resolved_end_no,
            start_anchor=inp.resolved_start_anchor,
            end_anchor=inp.resolved_end_anchor,
            new_string=inp.new_string,
            tool_name=self.id,
        )


def _resolve_edit_target(ctx: ToolContext, file_path: str):
    path = resolve_safe(ctx.workspace, file_path, ctx.sandbox_extra_paths)
    if path is None:
        return None, ToolResult(output=f"Path traversal blocked: {file_path}", metadata={"error": True})
    if not path.exists():
        return None, ToolResult(output=f"File not found: {file_path}", metadata={"error": True})
    stale = check_staleness(ctx, path)
    if stale:
        return None, ToolResult(output=stale, metadata={"error": True})
    return path, None


def _auto_create_file(
    ctx: ToolContext,
    *,
    file_path: str,
    new_string: str,
) -> ToolResult:
    path = resolve_safe(ctx.workspace, file_path, ctx.sandbox_extra_paths)
    if path is None:
        return ToolResult(output=f"Path traversal blocked: {file_path}", metadata={"error": True})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_string, encoding="utf-8")
    except OSError as exc:
        return ToolResult(
            output=f"Failed to create file: {file_path}\n{exc}",
            metadata={"error": True},
        )
    record_mtime(ctx, path)
    clear_read_coverage(ctx, path)
    diff = make_file_diff(file_path, "", new_string)
    file_diff = make_structured_diff(file_path, "", new_string)
    numbered_diff = render_numbered_diff(file_diff)
    output = f"File created: {file_path}"
    if numbered_diff:
        output = f"{output}\n{numbered_diff}"
    return ToolResult(
        title="File created",
        output=output,
        summary="File created",
        metadata={
            "file": file_path,
            "operations": 1,
            "auto_created": True,
        },
        diff=diff,
    )


async def _execute_text_replace(
    ctx: ToolContext,
    *,
    file_path: str,
    start_no: int,
    end_no: int,
    start_anchor: str,
    end_anchor: str,
    new_string: str,
    tool_name: str,
) -> ToolResult:
    path, error = _resolve_edit_target(ctx, file_path)
    if error is not None:
        if error.output.startswith("File not found:"):
            return _auto_create_file(ctx, file_path=file_path, new_string=new_string)
        _log_replace_failure(
            tool_name=tool_name,
            file_path=file_path,
            reason=error.output,
            ctx=ctx,
            start_no=start_no,
            end_no=end_no,
            start_anchor=start_anchor,
            end_anchor=end_anchor,
            new_string=new_string,
        )
        return error
    assert path is not None

    original = path.read_text(encoding="utf-8", errors="replace")
    display = _split_display_lines(original)
    drift_maps = get_line_drift_maps(ctx, path)
    fallback = _find_text_segment_with_drift_fallback(
        display.lines, start_no, end_no, start_anchor, end_anchor, drift_maps
    )
    if fallback.match is None:
        _log_replace_failure(
            tool_name=tool_name,
            file_path=file_path,
            reason=fallback.error or "text segment not found",
            ctx=ctx,
            start_no=start_no,
            end_no=end_no,
            start_anchor=start_anchor,
            end_anchor=end_anchor,
            new_string=new_string,
            lines=display.lines,
        )
        return ToolResult(output=fallback.error, metadata={"error": True})

    _, _, start_line, end_line = fallback.match
    drift_hint = ""
    if fallback.matched_map is not None and fallback.remapped_range is not None:
        drift_hint = (
            f"[Line drift fallback: {file_path} epoch #{fallback.matched_map.epoch} "
            f"start_no {start_no}→{fallback.remapped_range[0]}, "
            f"end_no {end_no}→{fallback.remapped_range[1]} matched via drift map.]\n"
        )
    coverage_error = check_read_coverage(ctx, path, start_line, end_line, display_path=file_path)
    if coverage_error:
        output = (
            f"{coverage_error}\n"
            f"Hint: Read lines {start_line}-{end_line}, then retry replace with the refreshed "
            "line numbers and anchors."
        )
        _log_replace_failure(
            tool_name=tool_name,
            file_path=file_path,
            reason=output,
            ctx=ctx,
            start_no=start_line,
            end_no=end_line,
            start_anchor=start_anchor,
            end_anchor=end_anchor,
            new_string=new_string,
            lines=display.lines,
        )
        return ToolResult(output=output, metadata={"error": True})

    key = str(path.resolve())
    existing_coverage = ctx.file_read_coverage.get(key, {})
    current_fingerprint = asdict(file_fingerprint(path))
    old_ranges = [
        item.copy()
        for item in existing_coverage.get("ranges", [])
    ] if existing_coverage.get("fingerprint") == current_fingerprint else []

    lines = list(display.lines)
    new_lines = _split_edit_lines(new_string)
    if (
        start_line == end_line
        and new_string in ("", "\n", " ")
        and lines[start_line - 1] != ""
    ):
        new_lines = []
    lines[start_line - 1:end_line] = new_lines

    # Head-line dedup: if the first line of new_string exactly matches the
    # line immediately before the replaced range, consume that line too.
    actual_start_line = start_line
    if new_lines and new_lines[0] != "":
        prev_idx = start_line - 2
        if prev_idx >= 0 and lines[prev_idx] == new_lines[0]:
            del lines[prev_idx]
            actual_start_line = start_line - 1

    # Tail-line dedup: if the last line of new_string exactly matches the
    # first line after the replaced range, consume that line too.
    actual_end_line = end_line
    if new_lines and new_lines[-1] != "":
        head_shift = 1 if actual_start_line < start_line else 0
        next_idx = start_line - 1 + len(new_lines) - head_shift
        if next_idx < len(lines) and lines[next_idx] == new_lines[-1]:
            del lines[next_idx]
            actual_end_line = end_line + 1

    # Trailing newline: preserve the original file's trailing newline.
    # _split_edit_lines already strips a trailing \n from new_string, so
    # both "foo" and "foo\n" produce the same line list.  The original
    # trailing newline is kept unless the file becomes empty.
    trailing_newline = display.trailing_newline if lines else False
    content = _join_display_lines(lines, trailing_newline=trailing_newline)

    await save_file_version(ctx, path, display_path=file_path, tool_name=tool_name)
    path.write_text(content, encoding="utf-8")
    diff = make_file_diff(file_path, original, content)
    file_diff = make_structured_diff(file_path, original, content)
    remap_read_coverage_from_file_diff(ctx, path, file_diff, old_ranges=old_ranges)

    numbered_diff = render_numbered_diff(file_diff)
    output = f"File edited: {file_path} (1 operations)"
    if drift_hint:
        output = f"{drift_hint}{output}"
    if numbered_diff:
        output = f"{output}\n{numbered_diff}"
    return ToolResult(
        title="Edited (1 edits)",
        output=output,
        summary="Edited (1 operations)",
        metadata={
            "file": file_path,
            "operations": 1,
            "start_line": actual_start_line,
            "end_line": actual_end_line,
        },
        diff=diff,
    )


async def _apply_resolved_edits(
    ctx: ToolContext,
    *,
    path,
    file_path: str,
    edits: list[ResolvedEdit],
    original: str,
    display: DisplayLines,
    tool_name: str,
    hints: list[str],
) -> ToolResult:
    lines = list(display.lines)
    total_lines = len(lines)
    validation_error = _validate_resolved_edits(edits, total_lines)
    if validation_error:
        _log_replace_failure(
            tool_name=tool_name,
            file_path=file_path,
            reason=validation_error,
            ctx=ctx,
            lines=display.lines,
        )
        return ToolResult(output=validation_error, metadata={"error": True})

    for i, edit in enumerate(edits):
        # (0, 0) is the convention for beginning-of-file insert/prepend —
        # no prior read is required since there are no existing lines to verify.
        # Similarly, insert at (total_lines, total_lines) is an append —
        # no existing lines are modified.
        if (edit.start_line, edit.end_line) == (0, 0):
            continue
        if edit.operation == "insert" and edit.start_line == total_lines and edit.end_line == total_lines:
            continue
        coverage_error = check_read_coverage(ctx, path, edit.start_line, edit.end_line, display_path=file_path)
        if coverage_error:
            output = (
                f"Edit {i}: {coverage_error}\n"
                f"Hint: Read lines {edit.start_line}-{edit.end_line}, then retry with the refreshed "
                "line numbers and anchors."
            )
            _log_replace_failure(
                tool_name=tool_name,
                file_path=file_path,
                reason=output,
                ctx=ctx,
                start_no=edit.start_line,
                end_no=edit.end_line,
                lines=display.lines,
            )
            return ToolResult(output=output, metadata={"error": True})

    key = str(path.resolve())
    existing_coverage = ctx.file_read_coverage.get(key, {})
    current_fingerprint = asdict(file_fingerprint(path))
    old_ranges = [
        item.copy()
        for item in existing_coverage.get("ranges", [])
    ] if existing_coverage.get("fingerprint") == current_fingerprint else []

    trailing_newline = _result_trailing_newline(edits, total_lines, display.trailing_newline)
    for edit in sorted(edits, key=lambda item: item.start_line, reverse=True):
        new_lines = _split_edit_lines(edit.new_string)
        if edit.operation == "replace":
            if (edit.start_line, edit.end_line) == (0, 0):
                lines[0:0] = new_lines
            else:
                lines[edit.start_line - 1:edit.end_line] = new_lines
        else:
            lines[edit.start_line:edit.start_line] = new_lines

    content = _join_display_lines(lines, trailing_newline=trailing_newline)

    await save_file_version(ctx, path, display_path=file_path, tool_name=tool_name)
    path.write_text(content, encoding="utf-8")
    diff = make_file_diff(file_path, original, content)
    file_diff = make_structured_diff(file_path, original, content)
    remap_read_coverage_from_file_diff(ctx, path, file_diff, old_ranges=old_ranges)

    numbered_diff = render_numbered_diff(file_diff)
    details = "\n".join([*hints, *_line_shift_hints(edits), numbered_diff])
    output = f"File edited: {file_path} ({len(edits)} operations)"
    if details:
        output = f"{output}\n{details}"

    return ToolResult(
        title=f"Edited ({len(edits)} edits)",
        output=output,
        summary=f"Edited ({len(edits)} operations)",
        metadata={"file": file_path, "operations": len(edits)},
        diff=diff,
    )


def _line_shift_hints(edits: list[ResolvedEdit]) -> list[str]:
    hints: list[str] = []
    for edit in sorted(edits, key=lambda item: item.start_line):
        new_count = len(_split_edit_lines(edit.new_string))
        if edit.operation == "replace":
            old_count = 0 if (edit.start_line, edit.end_line) == (0, 0) else edit.end_line - edit.start_line + 1
            offset = new_count - old_count
            if offset:
                if edit.end_line == 0:
                    hints.append(f"Line shift: all existing lines shifted by {offset:+d}")
                else:
                    hints.append(f"Line shift: lines after {edit.end_line} shifted by {offset:+d}")
        else:
            if new_count == 0:
                continue
            if edit.start_line == 0:
                hints.append(f"Line shift: all existing lines shifted by {new_count:+d}")
            else:
                hints.append(f"Line shift: lines after {edit.start_line} shifted by {new_count:+d}")
    return hints


class DriftFallbackResult(NamedTuple):
    match: tuple[int, int, int, int] | None
    error: str | None
    matched_map: LineDriftMap | None
    remapped_range: tuple[int, int] | None


def _find_text_segment_with_drift_fallback(
    lines: list[str],
    start_no: int,
    end_no: int,
    prefix: str,
    suffix: str,
    maps: list[LineDriftMap],
) -> DriftFallbackResult:
    first = _find_text_segment(lines, start_no, end_no, prefix, suffix)
    if not isinstance(first, str):
        return DriftFallbackResult(match=first, error=None, matched_map=None, remapped_range=None)

    if not maps:
        return DriftFallbackResult(match=None, error=first, matched_map=None, remapped_range=None)

    candidates: list[tuple[tuple[int, int, int, int], LineDriftMap, tuple[int, int]]] = []
    for m in sorted(maps, key=lambda x: x.epoch, reverse=True):
        remapped = remap_line_range(start_no, end_no, m.span_steps)
        if remapped is None or remapped == (start_no, end_no):
            continue
        result = _find_text_segment(lines, remapped[0], remapped[1], prefix, suffix)
        if not isinstance(result, str):
            candidates.append((result, m, remapped))

    if not candidates:
        return DriftFallbackResult(match=None, error=first, matched_map=None, remapped_range=None)

    if len(candidates) == 1:
        match, m, remapped = candidates[0]
        return DriftFallbackResult(match=match, error=None, matched_map=m, remapped_range=remapped)

    # 多候选:检查 resolved range 是否相同
    first_range = (candidates[0][0][2], candidates[0][0][3])
    if all((c[0][2], c[0][3]) == first_range for c in candidates):
        match, m, remapped = candidates[0]
        return DriftFallbackResult(match=match, error=None, matched_map=m, remapped_range=remapped)

    ranges_str = ", ".join(f"{c[0][2]}-{c[0][3]}" for c in candidates)
    return DriftFallbackResult(
        match=None,
        error=f"replace range is ambiguous after drift fallback: candidate ranges {ranges_str}. Please re-read the file.",
        matched_map=None,
        remapped_range=None,
    )
