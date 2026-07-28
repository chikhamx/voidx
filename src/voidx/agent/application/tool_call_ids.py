"""Helpers for extracting tool call ids from assistant messages."""

from __future__ import annotations

from langchain_core.messages import AIMessage


def ai_tool_call_ids(message: AIMessage) -> list[str]:
    ids: list[str] = []

    for call in getattr(message, "tool_calls", None) or []:
        if isinstance(call, dict) and call.get("id"):
            ids.append(str(call["id"]))

    content = message.content
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_use" and item.get("id"):
                ids.append(str(item["id"]))

    raw_calls = (getattr(message, "additional_kwargs", {}) or {}).get("tool_calls")
    if isinstance(raw_calls, list):
        for call in raw_calls:
            if isinstance(call, dict) and call.get("id"):
                ids.append(str(call["id"]))

    result: list[str] = []
    seen: set[str] = set()
    for tool_call_id in ids:
        if tool_call_id not in seen:
            result.append(tool_call_id)
            seen.add(tool_call_id)
    return result
