"""Tool exchange replay sanitization helpers."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from voidx.agent.tool_call_ids import ai_tool_call_ids
from voidx.llm.message_status import message_status


def sanitize_failed_tool_exchanges(
    messages: list[BaseMessage],
    *,
    preserve_latest: bool = False,
) -> list[BaseMessage]:
    failed_ids = _failed_tool_call_ids(messages)
    if preserve_latest:
        failed_ids.difference_update(_latest_failed_tool_exchange_ids(messages))
    if not failed_ids:
        return messages

    sanitized: list[BaseMessage] = []
    for message in messages:
        if isinstance(message, AIMessage):
            cleaned = _sanitize_ai_failed_calls(message, failed_ids)
            if cleaned is not None:
                sanitized.append(cleaned)
            continue

        if isinstance(message, ToolMessage):
            tool_call_id = str(getattr(message, "tool_call_id", "") or "")
            if tool_call_id and tool_call_id in failed_ids:
                continue

        sanitized.append(message)

    return sanitized


def _failed_tool_call_ids(messages: list[BaseMessage]) -> set[str]:
    failed: set[str] = set()
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        if message_status(getattr(message, "status", None)) != "error":
            continue
        tool_call_id = str(getattr(message, "tool_call_id", "") or "")
        if tool_call_id:
            failed.add(tool_call_id)
    return failed


def _latest_failed_tool_exchange_ids(messages: list[BaseMessage]) -> set[str]:
    trailing_failed_ids: set[str] = set()
    index = len(messages) - 1
    while index >= 0 and isinstance(messages[index], ToolMessage):
        message = messages[index]
        tool_call_id = str(getattr(message, "tool_call_id", "") or "")
        if tool_call_id and message_status(getattr(message, "status", None)) == "error":
            trailing_failed_ids.add(tool_call_id)
        index -= 1

    if not trailing_failed_ids or index < 0 or not isinstance(messages[index], AIMessage):
        return set()

    ai_ids = set(ai_tool_call_ids(messages[index]))
    return trailing_failed_ids.intersection(ai_ids)


def _sanitize_ai_failed_calls(message: AIMessage, failed_ids: set[str]) -> AIMessage | None:
    kept_calls: list[dict[str, Any]] = []
    calls_changed = False
    for call in getattr(message, "tool_calls", None) or []:
        if not isinstance(call, dict):
            kept_calls.append(call)
            continue
        call_id = str(call.get("id") or "")
        if call_id and call_id in failed_ids:
            calls_changed = True
            continue
        kept_calls.append(call)

    content, content_changed = _sanitize_ai_content(message.content, failed_ids)
    additional_kwargs, kwargs_changed = _sanitize_ai_additional_kwargs(
        getattr(message, "additional_kwargs", {}) or {},
        failed_ids,
    )

    if not calls_changed and not content_changed and not kwargs_changed:
        return message

    if _is_empty_content(content) and not kept_calls:
        return None

    update: dict[str, Any] = {
        "content": content,
        "tool_calls": kept_calls,
    }
    if kwargs_changed:
        update["additional_kwargs"] = additional_kwargs
    return message.model_copy(update=update)


def _sanitize_ai_content(content: object, failed_ids: set[str]) -> tuple[object, bool]:
    if not isinstance(content, list):
        return content, False

    kept: list[object] = []
    changed = False
    for item in content:
        if isinstance(item, dict):
            block_id = str(item.get("id") or "")
            if item.get("type") == "tool_use" and block_id and block_id in failed_ids:
                changed = True
                continue
        kept.append(item)
    return kept, changed


def _sanitize_ai_additional_kwargs(
    additional_kwargs: dict[str, Any],
    failed_ids: set[str],
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
        call_id = str(call.get("id") or "")
        if call_id and call_id in failed_ids:
            changed = True
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
