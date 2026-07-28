"""Tool exchange replay sanitization helpers."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from voidx.agent.application.tool_call_ids import ai_tool_call_ids
from voidx.llm.message_status import message_status


def sanitize_failed_tool_exchanges(
    messages: list[BaseMessage],
    *,
    preserve_latest: bool = False,
    preserve_rounds: int = 1,
) -> list[BaseMessage]:
    failed_ids = _failed_tool_call_ids(messages)
    if preserve_latest:
        failed_ids.difference_update(_latest_failed_tool_exchange_ids(messages, rounds=preserve_rounds))
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


def _latest_failed_tool_exchange_ids(messages: list[BaseMessage], *, rounds: int = 1) -> set[str]:
    preserved: set[str] = set()
    index = len(messages) - 1
    rounds_found = 0

    while index >= 0 and rounds_found < rounds:
        # Collect trailing ToolMessages at current position
        round_failed: set[str] = set()
        while index >= 0 and isinstance(messages[index], ToolMessage):
            message = messages[index]
            tool_call_id = str(getattr(message, "tool_call_id", "") or "")
            if tool_call_id and message_status(getattr(message, "status", None)) == "error":
                round_failed.add(tool_call_id)
            index -= 1

        if not round_failed or index < 0 or not isinstance(messages[index], AIMessage):
            break

        ai_ids = set(ai_tool_call_ids(messages[index]))
        preserved.update(round_failed.intersection(ai_ids))
        rounds_found += 1
        index -= 1

    return preserved


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
