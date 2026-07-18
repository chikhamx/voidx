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


def create_resolver_model(model: BaseChatModel, config: ModelConfig) -> BaseChatModel:
    return provider.create_resolver_model(model, config)


def extract_thinking(chunk: AIMessageChunk, protocol: str) -> str:
    from voidx.llm.thinking import extract_thinking as _extract
    return _extract(chunk, protocol)


def get_context_limit(provider_name: str, protocol: str = "", context_window: int | None = None) -> int:
    return provider.get_context_limit(provider_name, protocol, context_window)


def get_resolver_structured_output_method(model: BaseChatModel) -> str | None:
    method = getattr(model, "resolver_structured_output_method", None)
    return method if method in {"json_mode", "function_calling"} else None

__all__ = [
    "create_chat_model",
    "create_resolver_model",
    "extract_thinking",
    "get_context_limit",
    "get_resolver_structured_output_method",
    "resolve_protocol",
]
