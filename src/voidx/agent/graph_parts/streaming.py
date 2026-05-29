"""LLM streaming helpers for agent execution."""

from __future__ import annotations

from langchain_core.messages import AIMessage, AIMessageChunk

from voidx.ui.console import StreamingRenderer


async def stream_llm(
    model,
    messages: list,
    renderer: StreamingRenderer,
    protocol: str = "",
) -> AIMessage:
    """Stream LLM response, render live, return merged AIMessage."""
    from voidx.llm.provider import extract_thinking

    chunks: list[AIMessageChunk] = []
    renderer.start()

    try:
        async for chunk in model.astream(messages):
            chunks.append(chunk)
            content = chunk.content
            if isinstance(content, str) and content:
                renderer.feed_text(content)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        renderer.feed_text(item.get("text", ""))
            thinking = extract_thinking(chunk, protocol)
            if thinking:
                renderer.feed_thinking(thinking)
    except Exception:
        renderer.discard()
        raise
    finally:
        renderer.done()

    if not chunks:
        return AIMessage(content="")

    merged = chunks[0]
    for c in chunks[1:]:
        merged = merged + c

    return AIMessage(
        content=merged.content,
        tool_calls=merged.tool_calls,
        response_metadata=merged.response_metadata,
        additional_kwargs=merged.additional_kwargs,
    )


def extract_text(msg) -> str:
    content = msg.content if hasattr(msg, "content") else str(msg)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "".join(parts)
    return str(content)
