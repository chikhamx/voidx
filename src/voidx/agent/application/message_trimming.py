"""Token-bounded lazy trimming of superseded file tool messages.

Applied at compile_messages stage; does not mutate state messages.
"""

from __future__ import annotations

import json
import re

from collections.abc import Callable
from dataclasses import dataclass

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from voidx.agent.domain.file_ranges import DiffSpan, remap_old_range

COVERAGE_THRESHOLD = 0.6

_HUNK_HEADER_RE = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@")


def parse_read_line_range(content: str) -> tuple[int, int] | None:
    """Parse start/end line numbers from a read ToolMessage content.

    Read output format is ``{line_number}\\t{line}``. Returns (start, end) using
    the first and last line numbers, or None if content is empty or the first
    line has no parseable line number.
    """
    if not content:
        return None
    lines = content.split("\n")
    for line in lines:
        if not line:
            continue
        tab = line.find("\t")
        if tab <= 0:
            return None
        try:
            start = int(line[:tab])
        except ValueError:
            return None
        break
    else:
        return None

    end = start
    for line in reversed(lines):
        if not line:
            continue
        tab = line.find("\t")
        if tab <= 0:
            continue
        try:
            end = int(line[:tab])
        except ValueError:
            continue
        break
    return (start, end)


def parse_diff_hunk_ranges(content: str) -> list[tuple[int, int]]:
    """Parse changed-line ranges (new-file line numbers) from a unified diff.

    Returns merged ranges. Hunks with new_count == 0 (pure deletion) produce no
    range. new_count omitted defaults to 1.
    """
    ranges: list[tuple[int, int]] = []
    for line in content.split("\n"):
        m = _HUNK_HEADER_RE.match(line)
        if not m:
            continue
        new_start = int(m.group(3))
        new_count = int(m.group(4)) if m.group(4) is not None else 1
        if new_count <= 0:
            continue
        ranges.append((new_start, new_start + new_count - 1))
    return merge_ranges(ranges)


def merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping/adjacent ranges into a sorted disjoint list."""
    if not ranges:
        return []
    sorted_ranges = sorted(ranges, key=lambda r: r[0])
    merged: list[tuple[int, int]] = [sorted_ranges[0]]
    for start, end in sorted_ranges[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end + 1:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def coverage_ratio(
    target_ranges: list[tuple[int, int]],
    union: list[tuple[int, int]],
) -> float:
    """Fraction of target_ranges (in lines) covered by union."""
    total = sum(end - start + 1 for start, end in target_ranges)
    if total <= 0:
        return 0.0
    covered = 0
    for t_start, t_end in target_ranges:
        for u_start, u_end in union:
            overlap_start = max(t_start, u_start)
            overlap_end = min(t_end, u_end)
            if overlap_start <= overlap_end:
                covered += overlap_end - overlap_start + 1
    return covered / total


def build_diff_spans_from_text(content: str) -> list[DiffSpan]:
    """Build DiffSpan list from unified diff text for remap.

    Each hunk header ``@@ -old_start,old_count +new_start,new_count @@`` yields
    DiffSpan(old_start, old_start + old_count - 1, new_count - old_count).
    old_count/new_count omitted default to 1. Sorted by old_start.
    """
    spans: list[DiffSpan] = []
    for line in content.split("\n"):
        m = _HUNK_HEADER_RE.match(line)
        if not m:
            continue
        old_start = int(m.group(1))
        old_count = int(m.group(2)) if m.group(2) is not None else 1
        new_count = int(m.group(4)) if m.group(4) is not None else 1
        old_end = old_start + old_count - 1
        spans.append(DiffSpan(old_start=old_start, old_end=old_end, offset=new_count - old_count))
    spans.sort(key=lambda s: s.old_start)
    return spans


def _format_changed_lines(ranges: list[tuple[int, int]]) -> str:
    if not ranges:
        return "Changed lines: (deletion only)"
    if len(ranges) == 1:
        s, e = ranges[0]
        return f"Changed lines: {s}-{e}"
    return "Changed lines: " + ", ".join(f"{s}-{e}" for s, e in ranges)


def summarize_edit_diff(content: str) -> str:
    """Summarize a replace/write ToolMessage diff content.

    Keeps the ``File edited:`` header and ``Line shift:`` hint lines, drops all
    diff lines (``@@``/``+``/``-``/context), and appends ``Changed lines:``.
    """
    lines = content.split("\n")
    kept: list[str] = []
    for line in lines:
        if not line:
            continue
        if line.startswith("@@"):
            continue
        if line.startswith("+") or line.startswith("-"):
            continue
        if line.startswith(" "):
            continue
        kept.append(line)
    has_hunk = any(_HUNK_HEADER_RE.match(line) for line in lines)
    if has_hunk:
        hunk_ranges = parse_diff_hunk_ranges(content)
        kept.append(_format_changed_lines(hunk_ranges))
    return "\n".join(kept)


def remap_ranges(
    ranges: list[tuple[int, int]],
    spans: list[DiffSpan],
) -> list[tuple[int, int]]:
    """Remap line ranges through diff spans.

    Returns merged remapped ranges. Empty if fully deleted.
    """
    remapped: list[tuple[int, int]] = []
    for start, end in ranges:
        remapped.extend(remap_old_range(start, end, spans))
    return merge_ranges(remapped)


# ---------------------------------------------------------------------------
# Data records for window scanning
# ---------------------------------------------------------------------------


@dataclass
class ReadRecord:
    msg_index: int
    tool_call_id: str
    ranges: list[tuple[int, int]]
    deleted: bool = False


@dataclass
class EditRecord:
    msg_index: int
    tool_call_id: str
    hunk_ranges: list[tuple[int, int]]
    summarized: bool = False


# ---------------------------------------------------------------------------
# Rewrite bounds
# ---------------------------------------------------------------------------

DEFAULT_CACHE_INVALIDATION_TOKEN_BUDGET = 4096


def _message_line_count(msg: BaseMessage) -> int:
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content.count("\n") + 1 if content else 0
    if isinstance(content, list):
        total = 0
        for item in content:
            if isinstance(item, str):
                total += item.count("\n") + 1
            elif isinstance(item, dict):
                text = item.get("text", "")
                if isinstance(text, str):
                    total += text.count("\n") + 1
        return total
    return 0


def _compute_line_window_start(messages: list[BaseMessage], window_lines: int) -> int:
    """Return start index of the legacy line window without splitting tool pairs."""
    total = 0
    boundary = len(messages)
    for i in range(len(messages) - 1, -1, -1):
        total += _message_line_count(messages[i])
        if total >= window_lines:
            boundary = i
            while boundary > 0 and isinstance(messages[boundary], ToolMessage):
                boundary -= 1
            return boundary
    return 0


def _fallback_message_token_estimate(message: BaseMessage) -> int:
    payload = {
        "type": getattr(message, "type", ""),
        "content": getattr(message, "content", ""),
        "tool_calls": getattr(message, "tool_calls", None),
    }
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    return max(1, (len(serialized.encode("utf-8")) + 3) // 4)


def _tool_group_end(messages: list[BaseMessage], ai_index: int) -> int:
    call_ids = set(_collect_tool_call_info(messages[ai_index]))
    end = ai_index
    for index in range(ai_index + 1, len(messages)):
        message = messages[index]
        if not isinstance(message, ToolMessage):
            break
        if str(message.tool_call_id) not in call_ids:
            break
        end = index
    return end


def _message_token_prefix(
    messages: list[BaseMessage],
    token_estimator: Callable[[BaseMessage], int],
) -> list[int]:
    prefix = [0]
    for message in messages:
        try:
            tokens = max(int(token_estimator(message)), 0)
        except Exception:
            tokens = _fallback_message_token_estimate(message)
        prefix.append(prefix[-1] + tokens)
    return prefix


def _span_within_token_budget(
    token_prefix: list[int],
    start: int,
    end: int,
    token_budget: int,
) -> bool:
    return token_prefix[end + 1] - token_prefix[start] <= token_budget


# ---------------------------------------------------------------------------
# Pairing index
# ---------------------------------------------------------------------------

def _collect_tool_call_info(ai: AIMessage) -> dict[str, dict]:
    """Return {id: {name, args}} from all tool call shapes.

    Canonical source is ``ai.tool_calls``; falls back to
    ``additional_kwargs["tool_calls"]`` and content-list raw ``tool_use``
    blocks for providers that only populate the raw form.
    """
    info: dict[str, dict] = {}
    for tc in ai.tool_calls or []:
        if isinstance(tc, dict) and tc.get("id"):
            info[tc["id"]] = {"name": tc.get("name", ""), "args": tc.get("args", {})}

    raw_calls = (getattr(ai, "additional_kwargs", {}) or {}).get("tool_calls")
    if isinstance(raw_calls, list):
        for rc in raw_calls:
            if not isinstance(rc, dict) or not rc.get("id"):
                continue
            tc_id = rc["id"]
            if tc_id in info:
                continue
            info[tc_id] = {"name": rc.get("name", ""), "args": rc.get("args", {})}

    content = ai.content
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_use" or not block.get("id"):
                continue
            tc_id = block["id"]
            if tc_id in info:
                continue
            info[tc_id] = {"name": block.get("name", ""), "args": block.get("input", {})}

    return info


# ---------------------------------------------------------------------------
# Main trimming function
# ---------------------------------------------------------------------------

def trim_superseded_file_tools(
    messages: list[BaseMessage],
    *,
    token_budget: int = DEFAULT_CACHE_INVALIDATION_TOKEN_BUDGET,
    token_estimator: Callable[[BaseMessage], int] | None = None,
    window_lines: int | None = None,
) -> list[BaseMessage]:
    """Trim superseded file tools within a bounded rewrite span.

    Rule 1: delete old read tool_call + ToolMessage when covered ≥60% by
            subsequent retained reads of the same file.
    Rule 2: summarize edit diff when covered ≥60% by subsequent retained reads.
    The default token budget bounds the span from the rewritten tool group through
    the triggering tool group, limiting how much cached history can be invalidated.
    Explicit ``window_lines`` remains available for compatibility and focused tests.
    Does not mutate input; returns a new list.
    """
    if not messages:
        return list(messages)

    token_prefix: list[int] | None = None
    if window_lines is not None:
        window_start = _compute_line_window_start(messages, window_lines)
    else:
        window_start = 0
        estimator = token_estimator or _fallback_message_token_estimate
        token_prefix = _message_token_prefix(messages, estimator)
    bounded_token_budget = max(token_budget, 0)

    def rewrite_allowed(start: int, trigger: int) -> bool:
        if token_prefix is None:
            return True
        return _span_within_token_budget(
            token_prefix,
            start,
            _tool_group_end(messages, trigger),
            bounded_token_budget,
        )

    # Build pairing index within window.
    # tool_call_info: id → {ai_index, name, args}
    # tool_result: id → {tool_index, content, status}
    tool_call_info: dict[str, dict] = {}
    tool_result: dict[str, dict] = {}

    for i in range(window_start, len(messages)):
        msg = messages[i]
        if isinstance(msg, AIMessage):
            for tc_id, info in _collect_tool_call_info(msg).items():
                tool_call_info[tc_id] = {"ai_index": i, **info}
        elif isinstance(msg, ToolMessage):
            if msg.tool_call_id not in tool_result:
                tool_result[msg.tool_call_id] = {
                    "tool_index": i,
                    "content": msg.content,
                    "status": getattr(msg, "status", "success"),
                }

    # Only consider ids with both a tool_call and a successful ToolMessage.
    paired_ids = {
        tc_id for tc_id in tool_call_info
        if tc_id in tool_result and tool_result[tc_id]["status"] == "success"
    }

    # Per-file records.
    file_read_records: dict[str, list[ReadRecord]] = {}
    file_edit_records: dict[str, list[EditRecord]] = {}

    # Deletions / summarizations to apply.
    read_to_delete: set[str] = set()       # tool_call_ids
    edit_to_summarize: set[str] = set()    # tool_call_ids

    def retained_union(file_path: str, after_msg_index: int, exclude_id: str | None = None) -> list[tuple[int, int]]:
        ranges: list[tuple[int, int]] = []
        for rec in file_read_records.get(file_path, []):
            if rec.deleted:
                continue
            if rec.msg_index <= after_msg_index:
                continue
            if rec.tool_call_id == exclude_id:
                continue
            ranges.extend(rec.ranges)
        return merge_ranges(ranges)

    # Forward scan of window.
    for i in range(window_start, len(messages)):
        msg = messages[i]
        if not isinstance(msg, AIMessage):
            continue
        tc_info = _collect_tool_call_info(msg)
        for tc_id, info in tc_info.items():
            if tc_id not in paired_ids:
                continue
            name = info["name"]
            args = info["args"]
            file_path = args.get("file_path")
            if not file_path:
                continue

            if name == "read":
                content = tool_result[tc_id]["content"]
                if not isinstance(content, str):
                    continue
                line_range = parse_read_line_range(content)
                if line_range is None:
                    continue
                rec = ReadRecord(msg_index=i, tool_call_id=tc_id, ranges=[line_range])
                file_read_records.setdefault(file_path, []).append(rec)

                # Check earlier reads for coverage deletion.
                for older in file_read_records.get(file_path, []):
                    if older is rec or older.deleted:
                        continue
                    if older.msg_index >= i:
                        continue
                    union = retained_union(file_path, after_msg_index=older.msg_index, exclude_id=older.tool_call_id)
                    if (
                        rewrite_allowed(older.msg_index, i)
                        and coverage_ratio(older.ranges, union) >= COVERAGE_THRESHOLD
                    ):
                        older.deleted = True
                        read_to_delete.add(older.tool_call_id)

                # Check edits for summarization (rule 2).
                for edit_rec in file_edit_records.get(file_path, []):
                    if edit_rec.summarized:
                        continue
                    if edit_rec.msg_index >= i:
                        continue
                    union = retained_union(file_path, after_msg_index=edit_rec.msg_index)
                    if (
                        rewrite_allowed(edit_rec.msg_index, i)
                        and coverage_ratio(edit_rec.hunk_ranges, union) >= COVERAGE_THRESHOLD
                    ):
                        edit_rec.summarized = True
                        edit_to_summarize.add(edit_rec.tool_call_id)

            elif name in {"replace", "write"}:
                content = tool_result[tc_id]["content"]
                if not isinstance(content, str):
                    continue
                hunk_ranges = parse_diff_hunk_ranges(content)
                edit_rec = EditRecord(msg_index=i, tool_call_id=tc_id, hunk_ranges=hunk_ranges)
                file_edit_records.setdefault(file_path, []).append(edit_rec)

                # Remap existing reads and earlier edits through this diff.
                spans = build_diff_spans_from_text(content)
                if spans:
                    for older_read in file_read_records.get(file_path, []):
                        if older_read.deleted or older_read.msg_index >= i:
                            continue
                        remapped = remap_ranges(older_read.ranges, spans)
                        if not remapped and rewrite_allowed(older_read.msg_index, i):
                            older_read.deleted = True
                            read_to_delete.add(older_read.tool_call_id)
                        else:
                            older_read.ranges = remapped
                    for older_edit in file_edit_records.get(file_path, []):
                        if older_edit is edit_rec or older_edit.summarized:
                            continue
                        if older_edit.msg_index >= i:
                            continue
                        remapped = remap_ranges(older_edit.hunk_ranges, spans)
                        older_edit.hunk_ranges = remapped

    # Build result list.
    result: list[BaseMessage] = []
    # Track AIMessages that need tool_calls update.
    ai_replacements: dict[int, AIMessage] = {}

    for i in range(len(messages)):
        if i < window_start:
            result.append(messages[i])
            continue

        msg = messages[i]

        if isinstance(msg, ToolMessage):
            if msg.tool_call_id in read_to_delete:
                continue  # deleted read
            if msg.tool_call_id in edit_to_summarize:
                content = msg.content
                if isinstance(content, str):
                    new_content = summarize_edit_diff(content)
                    result.append(msg.model_copy(update={"content": new_content}))
                    continue
            result.append(msg)
            continue

        if isinstance(msg, AIMessage):
            tc_info = _collect_tool_call_info(msg)
            deleted_in_this_ai = [
                tc_id for tc_id in tc_info
                if tc_id in read_to_delete
            ]
            if not deleted_in_this_ai:
                result.append(msg)
                continue

            # Need to rebuild tool_calls + additional_kwargs + content-list.
            remaining_ids = [tc_id for tc_id in tc_info if tc_id not in read_to_delete]
            new_tool_calls = [tc for tc in (msg.tool_calls or []) if tc.get("id") not in read_to_delete]

            # Compute filtered content-list first (raw tool_use blocks removed).
            filtered_content = msg.content
            if isinstance(msg.content, list):
                filtered_content = [
                    block for block in msg.content
                    if not (
                        isinstance(block, dict)
                        and block.get("type") == "tool_use"
                        and block.get("id") in read_to_delete
                    )
                ]

            # If no remaining tool_calls and no text content → drop AIMessage.
            has_text = bool(filtered_content) and not (
                isinstance(filtered_content, str) and filtered_content == ""
            )
            if not remaining_ids and not has_text:
                continue

            new_ai = msg.model_copy(update={
                "tool_calls": new_tool_calls,
                "content": filtered_content,
            })

            # Sync additional_kwargs["tool_calls"].
            raw_calls = (new_ai.additional_kwargs or {}).get("tool_calls")
            if isinstance(raw_calls, list):
                filtered_raw = [rc for rc in raw_calls if rc.get("id") not in read_to_delete]
                new_kwargs = dict(new_ai.additional_kwargs or {})
                new_kwargs["tool_calls"] = filtered_raw
                new_ai = new_ai.model_copy(update={"additional_kwargs": new_kwargs})

            result.append(new_ai)
            continue

        result.append(msg)

    return result
