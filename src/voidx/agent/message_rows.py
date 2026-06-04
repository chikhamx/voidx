"""Hydrate persisted message rows into LangChain messages."""

from __future__ import annotations

from collections.abc import Iterable

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from voidx.agent.attachments import parse_structured_content
from voidx.memory.session import MessageRow


def messages_from_rows(rows: Iterable[MessageRow]) -> list[BaseMessage]:
    messages: list[BaseMessage] = []
    for row in rows:
        msg_id = str(row.id) if row.id is not None else None
        if row.role == "system":
            messages.append(SystemMessage(content=row.content, id=msg_id))
        elif row.role == "user":
            messages.append(HumanMessage(
                content=parse_structured_content(row.content, row.content_format),
                id=msg_id,
            ))
        elif row.role == "assistant":
            messages.append(AIMessage(
                content=parse_structured_content(row.content, row.content_format),
                tool_calls=row.tool_calls or [],
                id=msg_id,
            ))
        elif row.role == "tool":
            messages.append(ToolMessage(
                content=row.content,
                tool_call_id=row.tool_call_id or "",
                id=msg_id,
            ))
    return messages
