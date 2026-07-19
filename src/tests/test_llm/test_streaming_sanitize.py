"""Tests for _sanitize_messages_for_replay protocol-aware reasoning_content handling."""

import sys
from pathlib import Path


from langchain_core.messages import AIMessage, HumanMessage

from voidx.agent.infrastructure.langgraph.runtime.streaming import (
    _sanitize_ai_content_for_replay,
    _sanitize_messages_for_replay,
)


def test_sanitize_removes_reasoning_content_by_default():
    """Without protocol, reasoning_content blocks are stripped from AI messages."""
    msg = AIMessage(content=[
        {"type": "reasoning_content", "reasoning_content": "thinking..."},
        {"type": "text", "text": "answer"},
    ])
    result = _sanitize_ai_content_for_replay(msg.content)
    assert result == "answer"


def test_sanitize_preserves_reasoning_content_for_deepseek():
    """DeepSeek protocol requires reasoning_content to be passed back in multi-turn."""
    msg = AIMessage(content=[
        {"type": "reasoning_content", "reasoning_content": "thinking..."},
        {"type": "text", "text": "answer"},
    ])
    result = _sanitize_ai_content_for_replay(msg.content, protocol="deepseek")
    assert isinstance(result, list)
    types = [b.get("type") for b in result if isinstance(b, dict)]
    assert "reasoning_content" in types
    assert "text" in types


def test_sanitize_preserves_reasoning_content_for_deepseek_only_content():
    """DeepSeek: AI message with only reasoning_content is NOT dropped."""
    msg = AIMessage(content=[
        {"type": "reasoning_content", "reasoning_content": "thinking..."},
    ])
    result = _sanitize_messages_for_replay([msg], protocol="deepseek")
    assert len(result) == 1
    content = result[0].content
    types = [b.get("type") for b in content if isinstance(b, dict)]
    assert "reasoning_content" in types


def test_sanitize_drops_reasoning_only_message_without_deepseek():
    """Without deepseek protocol, AI message with only reasoning_content is dropped."""
    msg = AIMessage(content=[
        {"type": "reasoning_content", "reasoning_content": "thinking..."},
    ])
    result = _sanitize_messages_for_replay([msg])
    assert len(result) == 0


def test_sanitize_messages_preserves_reasoning_for_deepseek_protocol():
    """Full message list: deepseek protocol keeps reasoning_content in AI messages."""
    messages = [
        HumanMessage(content="hello"),
        AIMessage(content=[
            {"type": "reasoning_content", "reasoning_content": "let me think"},
            {"type": "text", "text": "hi there"},
        ]),
        HumanMessage(content="thanks"),
    ]
    result = _sanitize_messages_for_replay(messages, protocol="deepseek")
    assert len(result) == 3
    ai_msg = result[1]
    assert isinstance(ai_msg, AIMessage)
    types = [b.get("type") for b in ai_msg.content if isinstance(b, dict)]
    assert "reasoning_content" in types


def test_sanitize_messages_removes_reasoning_for_openai_protocol():
    """OpenAI protocol: reasoning_content blocks are stripped."""
    messages = [
        HumanMessage(content="hello"),
        AIMessage(content=[
            {"type": "reasoning_content", "reasoning_content": "let me think"},
            {"type": "text", "text": "hi there"},
        ]),
    ]
    result = _sanitize_messages_for_replay(messages, protocol="openai")
    assert len(result) == 2
    ai_msg = result[1]
    assert isinstance(ai_msg, AIMessage)
    assert ai_msg.content == "hi there"


def test_sanitize_preserves_thinking_blocks_for_deepseek():
    """DeepSeek protocol also preserves 'thinking' type blocks."""
    msg = AIMessage(content=[
        {"type": "thinking", "thinking": "deep thought"},
        {"type": "text", "text": "answer"},
    ])
    result = _sanitize_ai_content_for_replay(msg.content, protocol="deepseek")
    types = [b.get("type") for b in result if isinstance(b, dict)]
    assert "thinking" in types
    assert "text" in types
