"""Todo runtime state helpers and replay sanitization."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from voidx.agent.task_state import TodoRunItem, TodoRunState


_DSML_MARKER_RE = r"\|\|DSML\|\|"
# Tools whose ToolMessage should be sanitized on replay.
# This is the source of truth — display_policy.DEFAULT_DISPLAY_RULES references this set
# via REPLAY_SANITIZED_TOOL_NAMES to keep replay_sanitize flags in sync.
_REPLAY_SANITIZED_TOOL_NAMES = frozenset({
    "todo",
    "workflow",
})
_REPLAY_SANITIZED_TOOL_PATTERN = "|".join(sorted(map(re.escape, _REPLAY_SANITIZED_TOOL_NAMES)))
_DSML_RUNTIME_INVOKE_RE = re.compile(
    rf"<{_DSML_MARKER_RE}invoke\b(?=[^>]*\bname=\"(?:{_REPLAY_SANITIZED_TOOL_PATTERN})\")[^>]*>.*?</{_DSML_MARKER_RE}invoke>",
    re.DOTALL,
)


def todo_run_state_from_result(result: object) -> TodoRunState | None:
    metadata = getattr(result, "metadata", {}) or {}
    
    # Short-circuit for read operations
    todo_op = metadata.get("todo_op")
    if todo_op == "read":
        return None
    
    raw_items = metadata.get("todo_items")
    summary = metadata.get("todo_summary")
    if not isinstance(raw_items, list) or not isinstance(summary, str):
        return None
    try:
        items = [TodoRunItem.model_validate(item) for item in raw_items]
    except Exception:
        return None
    
    # Build counts
    total = len(items)
    done = sum(1 for item in items if item.status == "completed")
    in_progress = sum(1 for item in items if item.status == "in_progress")
    pending = sum(1 for item in items if item.status == "pending")
    cancelled = sum(1 for item in items if item.status == "cancelled")
    
    # Build active_items (only in_progress)
    active_items = [item for item in items if item.status == "in_progress"]
    
    return TodoRunState(
        summary=summary,
        total=total,
        done=done,
        in_progress=in_progress,
        pending=pending,
        cancelled=cancelled,
        active_items=active_items,
        updated_at=datetime.now(timezone.utc).isoformat(),
    )


def apply_todo_state_to_host(host: object, raw_state: object) -> None:
    task_state = getattr(host, "_task_state", None)
    tracker = getattr(host, "_tracker", None)

    if raw_state is None:
        if task_state is not None:
            task_state.todo_state = None
        if tracker is not None:
            tracker.clear_todos()
        return

    try:
        todo_state = raw_state if isinstance(raw_state, TodoRunState) else TodoRunState.model_validate(raw_state)
    except (TypeError, ValueError):
        return

    if task_state is not None:
        task_state.todo_state = todo_state
    if tracker is not None:
        if todo_state.total > 0:
            # Convert active_items to dict format for tracker
            todos_dict = {}
            for item in todo_state.active_items:
                todos_dict[item.id] = {"content": item.content, "status": item.status}
            tracker.set_todos_from_dict(todos_dict)
        else:
            tracker.clear_todos()


def sanitize_todo_replay_messages(
    messages: list[BaseMessage],
    *,
    preserve_latest_tool_exchange: bool = False,
    preserve_trailing_ai_tool_calls: bool = False,
) -> list[BaseMessage]:
    """Remove runtime-only tool calls and matching tool results from semantic replay."""
    sanitized: list[BaseMessage] = []
    removed_tool_call_ids: set[str] = set()
    preserved_tool_call_ids = (
        _latest_runtime_tool_exchange_ids(messages)
        if preserve_latest_tool_exchange
        else set()
    )
    if preserve_trailing_ai_tool_calls:
        preserved_tool_call_ids.update(_trailing_ai_runtime_tool_call_ids(messages))

    for message in messages:
        if isinstance(message, AIMessage):
            cleaned = _sanitize_ai_runtime_calls(message, preserved_tool_call_ids)
            removed_tool_call_ids.update(cleaned.removed_ids)
            if cleaned.message is not None:
                sanitized.append(cleaned.message)
            continue

        if isinstance(message, ToolMessage):
            tool_call_id = str(getattr(message, "tool_call_id", "") or "")
            if tool_call_id in removed_tool_call_ids:
                continue

        sanitized.append(message)

    return sanitized


class _SanitizedAI:
    def __init__(self, message: AIMessage | None, removed_ids: set[str]) -> None:
        self.message = message
        self.removed_ids = removed_ids


def _sanitize_ai_runtime_calls(message: AIMessage, preserved_ids: set[str]) -> _SanitizedAI:
    removed_ids: set[str] = set()
    kept_calls: list[dict[str, Any]] = []
    calls_changed = False

    for call in getattr(message, "tool_calls", None) or []:
        if not isinstance(call, dict):
            kept_calls.append(call)
            continue
        call_id = str(call.get("id") or "")
        if call.get("name") in _REPLAY_SANITIZED_TOOL_NAMES and call_id not in preserved_ids:
            calls_changed = True
            if call_id:
                removed_ids.add(call_id)
            continue
        kept_calls.append(call)

    content, content_changed = _sanitize_runtime_tool_content(message.content, removed_ids, preserved_ids)
    additional_kwargs, kwargs_changed = _sanitize_runtime_tool_additional_kwargs(
        getattr(message, "additional_kwargs", {}) or {},
        removed_ids,
        preserved_ids,
    )

    if not calls_changed and not content_changed and not kwargs_changed:
        return _SanitizedAI(message, removed_ids)

    if _is_empty_content(content) and not kept_calls:
        return _SanitizedAI(None, removed_ids)

    update: dict[str, Any] = {
        "content": content,
        "tool_calls": kept_calls,
    }
    if kwargs_changed:
        update["additional_kwargs"] = additional_kwargs
    return _SanitizedAI(message.model_copy(update=update), removed_ids)


def _sanitize_runtime_tool_content(
    content: object,
    removed_ids: set[str],
    preserved_ids: set[str],
) -> tuple[object, bool]:
    if isinstance(content, str):
        cleaned = _DSML_RUNTIME_INVOKE_RE.sub("", content)
        return cleaned, cleaned != content
    if not isinstance(content, list):
        return content, False

    changed = False
    kept: list[object] = []
    for item in content:
        if isinstance(item, dict):
            block_type = item.get("type")
            block_id = str(item.get("id") or "")
            block_name = item.get("name")
            should_remove = block_type == "tool_use" and (
                block_name in _REPLAY_SANITIZED_TOOL_NAMES
                or (block_id and block_id in removed_ids)
            )
            if should_remove and block_id not in preserved_ids:
                changed = True
                if block_id:
                    removed_ids.add(block_id)
                continue
        kept.append(item)
    return kept, changed


def _sanitize_runtime_tool_additional_kwargs(
    additional_kwargs: dict[str, Any],
    removed_ids: set[str],
    preserved_ids: set[str],
) -> tuple[dict[str, Any], bool]:
    raw_calls = additional_kwargs.get("tool_calls")
    if not isinstance(raw_calls, list):
        return additional_kwargs, False

    kept: list[object] = []
    changed = False
    for call in raw_calls:
        if not isinstance(call, dict):
            kept.append(call)
            continue
        function = call.get("function")
        name = function.get("name") if isinstance(function, dict) else call.get("name")
        call_id = str(call.get("id") or "")
        should_remove = name in _REPLAY_SANITIZED_TOOL_NAMES or (call_id and call_id in removed_ids)
        if should_remove and call_id not in preserved_ids:
            changed = True
            if call_id:
                removed_ids.add(call_id)
            continue
        kept.append(call)

    if not changed:
        return additional_kwargs, False
    update = dict(additional_kwargs)
    if kept:
        update["tool_calls"] = kept
    else:
        update.pop("tool_calls", None)
    return update, True


def _latest_runtime_tool_exchange_ids(messages: list[BaseMessage]) -> set[str]:
    trailing_tool_ids: set[str] = set()
    index = len(messages) - 1
    while index >= 0 and isinstance(messages[index], ToolMessage):
        tool_call_id = str(getattr(messages[index], "tool_call_id", "") or "")
        if tool_call_id:
            trailing_tool_ids.add(tool_call_id)
        index -= 1

    if not trailing_tool_ids or index < 0 or not isinstance(messages[index], AIMessage):
        return set()

    preserved: set[str] = set()
    for call in getattr(messages[index], "tool_calls", None) or []:
        if not isinstance(call, dict):
            continue
        call_id = str(call.get("id") or "")
        if call_id in trailing_tool_ids and call.get("name") in _REPLAY_SANITIZED_TOOL_NAMES:
            preserved.add(call_id)
    return preserved


def _trailing_ai_runtime_tool_call_ids(messages: list[BaseMessage]) -> set[str]:
    if not messages or not isinstance(messages[-1], AIMessage):
        return set()
    preserved: set[str] = set()
    for call in getattr(messages[-1], "tool_calls", None) or []:
        if not isinstance(call, dict):
            continue
        call_id = str(call.get("id") or "")
        if call_id and call.get("name") in _REPLAY_SANITIZED_TOOL_NAMES:
            preserved.add(call_id)
    return preserved


def _is_empty_content(content: object) -> bool:
    if content is None:
        return True
    if isinstance(content, str):
        return content.strip() == ""
    if isinstance(content, list):
        return len(content) == 0
    return False
