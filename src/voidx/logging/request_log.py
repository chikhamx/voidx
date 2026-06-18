"""LLM request/response exchange logger.

Writes each LLM call's full context and response to a JSONL log file
so developers can inspect what was sent and received.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

logger = logging.getLogger(__name__)

_DEFAULT_LOG_DIR = Path.home() / ".voidx" / "logs"
_LOG_FILE_NAME = "llm_requests.jsonl"


def _serialize_message(msg: BaseMessage) -> dict[str, Any]:
    role = msg.__class__.__name__
    role_map = {
        "HumanMessage": "human",
        "AIMessage": "ai",
        "SystemMessage": "system",
        "ToolMessage": "tool",
    }
    role = role_map.get(role, role)

    content = msg.content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text", "")
                if text:
                    parts.append(text)
                elif item.get("type") == "text":
                    parts.append(item.get("text", ""))
        content = "\n".join(parts) if parts else str(content)

    result: dict[str, Any] = {"role": role, "content": content}

    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        result["tool_calls"] = tool_calls

    tool_call_id = getattr(msg, "tool_call_id", None)
    if tool_call_id:
        result["tool_call_id"] = tool_call_id

    name = getattr(msg, "name", None)
    if name:
        result["name"] = name

    return result


def serialize_llm_message(msg: BaseMessage) -> dict[str, Any]:
    return _serialize_message(msg)


def _serialize_response(msg: AIMessage) -> dict[str, Any]:
    content = msg.content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text", "")
                if text:
                    parts.append(text)
                elif item.get("type") == "text":
                    parts.append(item.get("text", ""))
        content = "\n".join(parts) if parts else str(content)

    result: dict[str, Any] = {"content": content}

    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        result["tool_calls"] = tool_calls

    usage_metadata = getattr(msg, "usage_metadata", None)
    if usage_metadata:
        result["usage"] = usage_metadata

    return result


def log_llm_exchange(
    messages: list[BaseMessage],
    response: AIMessage,
    *,
    model: str,
    provider: str,
    step: int,
    session_id: str | None = None,
    enabled: bool = True,
) -> None:
    if not enabled:
        return
    try:
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "model": model,
            "provider": provider,
            "step": step,
            "request": {
                "messages": [_serialize_message(m) for m in messages],
            },
            "response": _serialize_response(response),
        }
        if session_id is not None:
            entry["session_id"] = session_id

        _append_log_entry(entry)
    except Exception:
        logger.warning("Failed to write LLM request log", exc_info=True)


def log_llm_diagnostic(event: str, *, enabled: bool = True, **fields: Any) -> None:
    if not enabled:
        return
    try:
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **fields,
        }
        _append_log_entry(entry)
    except Exception:
        logger.warning("Failed to write LLM diagnostic log", exc_info=True)


def _append_log_entry(entry: dict[str, Any]) -> None:
    log_dir = _DEFAULT_LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / _LOG_FILE_NAME
    line = json.dumps(entry, ensure_ascii=False, default=str)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
