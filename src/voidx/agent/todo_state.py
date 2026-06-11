"""Todo runtime state helpers and replay sanitization."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from voidx.agent.task_state import TodoRunItem, TodoRunState


_DSML_MARKER_RE = r"\|\|DSML\|\|"
_DSML_TODO_INVOKE_RE = re.compile(
    rf"<{_DSML_MARKER_RE}invoke\b(?=[^>]*\bname=\"todo\")[^>]*>.*?</{_DSML_MARKER_RE}invoke>",
    re.DOTALL,
)


def todo_run_state_from_result(result: object) -> TodoRunState | None:
    metadata = getattr(result, "metadata", {}) or {}
    raw_items = metadata.get("todo_items")
    summary = metadata.get("todo_summary")
    if not isinstance(raw_items, list) or not isinstance(summary, str):
        return None
    try:
        items = [TodoRunItem.model_validate(item) for item in raw_items]
    except Exception:
        return None
    return TodoRunState(
        summary=summary,
        items=items,
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
        if todo_state.items:
            tracker.set_todos(todo_state.items)
        else:
            tracker.clear_todos()


def sanitize_todo_replay_messages(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Remove todo tool calls and matching tool results from semantic replay."""
    sanitized: list[BaseMessage] = []
    removed_tool_call_ids: set[str] = set()

    for message in messages:
        if isinstance(message, AIMessage):
            cleaned = _sanitize_ai_todo_calls(message)
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


def _sanitize_ai_todo_calls(message: AIMessage) -> _SanitizedAI:
    removed_ids: set[str] = set()
    kept_calls: list[dict[str, Any]] = []
    calls_changed = False

    for call in getattr(message, "tool_calls", None) or []:
        if not isinstance(call, dict):
            kept_calls.append(call)
            continue
        if call.get("name") == "todo":
            calls_changed = True
            call_id = call.get("id")
            if call_id:
                removed_ids.add(str(call_id))
            continue
        kept_calls.append(call)

    content, content_changed = _sanitize_todo_content(message.content, removed_ids)
    additional_kwargs, kwargs_changed = _sanitize_todo_additional_kwargs(
        getattr(message, "additional_kwargs", {}) or {},
        removed_ids,
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


def _sanitize_todo_content(content: object, removed_ids: set[str]) -> tuple[object, bool]:
    if isinstance(content, str):
        cleaned = _DSML_TODO_INVOKE_RE.sub("", content)
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
            if block_type == "tool_use" and (
                block_name == "todo" or (block_id and block_id in removed_ids)
            ):
                changed = True
                if block_id:
                    removed_ids.add(block_id)
                continue
        kept.append(item)
    return kept, changed


def _sanitize_todo_additional_kwargs(
    additional_kwargs: dict[str, Any],
    removed_ids: set[str],
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
        if name == "todo" or (call_id and call_id in removed_ids):
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


def _is_empty_content(content: object) -> bool:
    if content is None:
        return True
    if isinstance(content, str):
        return content.strip() == ""
    if isinstance(content, list):
        return len(content) == 0
    return False
