from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from voidx.tooling.domain.diff import FileDiff
from voidx.lsp.domain import LspError, LspFormattingUnsupported, LspPosition, LspRange
from voidx.lsp.ports.operations import LspOperations
from voidx.tooling.ports.post_edit import PostEditFormatter

from voidx.tooling.builtin.file.io import safe_read_text, safe_write_text
FormatAfterEditStatus = Literal[
    "disabled",
    "unavailable",
    "unsupported",
    "unchanged",
    "formatted",
    "failed",
]


@dataclass(frozen=True)
class FormatAfterEditResult:
    final_text: str | None
    status: FormatAfterEditStatus
    error: str = ""


@dataclass(frozen=True)
class LspPostEditFormatter:
    operations: LspOperations
    enabled: bool = True

    async def format_range(self, file_path: str, range_: object) -> tuple[bool, str, str]:
        return await self.operations.format_range(file_path, range_)


def format_range_from_diff(edited_text: str, file_diff: FileDiff) -> LspRange | None:
    if not edited_text or not file_diff.hunks:
        return None

    changed_lines: list[int] = []
    deletion_anchors: list[int] = []
    for hunk in file_diff.hunks:
        removed_pending = False
        previous_new_line: int | None = None
        for line in hunk.lines:
            if line.kind == "remove":
                removed_pending = True
                continue
            if line.new_lineno is None:
                continue
            zero_based = line.new_lineno - 1
            if line.kind == "add":
                changed_lines.append(zero_based)
                removed_pending = False
            elif removed_pending:
                deletion_anchors.append(zero_based)
                removed_pending = False
            previous_new_line = zero_based
        if removed_pending and previous_new_line is not None:
            deletion_anchors.append(previous_new_line)

    affected = [*changed_lines, *deletion_anchors]
    if not affected:
        return None
    start_line = min(affected)
    end_line = max(affected)
    lines = edited_text.splitlines(keepends=True)
    if end_line + 1 < len(lines) or edited_text.endswith(("\n", "\r")):
        end = LspPosition(line=end_line + 1, character=0)
    else:
        line_text = lines[end_line].rstrip("\r\n")
        end = LspPosition(line=end_line, character=_utf16_length(line_text))
    return LspRange(
        start=LspPosition(line=start_line, character=0),
        end=end,
    )


async def format_after_edit(
    formatter: PostEditFormatter | None,
    path: Path,
    *,
    display_path: str,
    edited_text: str,
    format_range: LspRange | None,
) -> FormatAfterEditResult:
    if formatter is not None and not formatter.enabled:
        return FormatAfterEditResult(edited_text, "disabled")
    if format_range is None or formatter is None:
        return FormatAfterEditResult(edited_text, "unavailable")
    try:
        changed, source_text, formatted_text = await formatter.format_range(
            display_path,
            format_range,
        )
    except LspFormattingUnsupported as exc:
        return _actual_file_state(path, _safe_error(exc), status="unsupported")
    except LspError as exc:
        return _actual_file_state(path, _safe_error(exc))
    except Exception as exc:
        return _actual_file_state(path, _safe_error(exc))
    if source_text != edited_text:
        return _actual_file_state(path, "file changed before formatting")
    if not changed or formatted_text == edited_text:
        return _actual_file_state(path, "", status="unchanged")
    write_error = safe_write_text(
        path,
        formatted_text,
        require_exists=True,
        expected_text=edited_text,
    )
    if write_error is not None:
        return _actual_file_state(path, write_error)
    return FormatAfterEditResult(formatted_text, "formatted")



def _actual_file_state(
    path: Path,
    error: str,
    *,
    status: FormatAfterEditStatus = "failed",
) -> FormatAfterEditResult:
    actual_text, read_error = safe_read_text(path)
    if read_error is not None:
        return FormatAfterEditResult(
            None,
            "failed",
            f"final file state unavailable: {read_error}",
        )
    return FormatAfterEditResult(actual_text, status, error)

def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _safe_error(exc: Exception) -> str:
    return str(exc).splitlines()[0][:200]
