"""Public LLM service boundary."""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessageChunk

from voidx.config import ModelConfig
from voidx.llm import provider
from voidx.llm.provider import DeepSeekChatOpenAI


def resolve_protocol(config: ModelConfig) -> str:
    return provider.resolve_protocol(config)


def create_chat_model(api_key: str, config: ModelConfig) -> BaseChatModel:
    return provider.create_chat_model(api_key, config)


def create_resolver_model(model: BaseChatModel, config: ModelConfig) -> BaseChatModel:
    return provider.create_resolver_model(model, config)


def extract_thinking(chunk: AIMessageChunk, protocol: str) -> str:
    from voidx.llm.thinking import extract_thinking as _extract
    return _extract(chunk, protocol)


def get_context_limit(provider_name: str, protocol: str = "", context_window: int | None = None) -> int:
    return provider.get_context_limit(provider_name, protocol, context_window)

__all__ = [
    "create_chat_model",
    "create_resolver_model",
    "DeepSeekChatOpenAI",
    "extract_thinking",
    "get_context_limit",
    "resolve_protocol",
]
