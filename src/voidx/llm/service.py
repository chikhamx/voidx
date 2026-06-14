"""Public LLM service boundary."""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessageChunk

from voidx.config import ModelConfig
from voidx.llm import provider


def resolve_protocol(config: ModelConfig) -> str:
    return provider.resolve_protocol(config)


def create_chat_model(api_key: str, config: ModelConfig) -> BaseChatModel:
    return provider.create_chat_model(api_key, config)


def extract_thinking(chunk: AIMessageChunk, protocol: str) -> str:
    return provider.extract_thinking(chunk, protocol)


def get_context_limit(provider_name: str, protocol: str = "") -> int:
    return provider.get_context_limit(provider_name, protocol)

__all__ = [
    "create_chat_model",
    "extract_thinking",
    "get_context_limit",
    "resolve_protocol",
]
