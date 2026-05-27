"""LLM Provider layer — typed abstraction over LangChain ChatModels."""

from __future__ import annotations

from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI
from langchain_core.language_models import BaseChatModel

from voidx.config import ModelConfig


def create_chat_model(api_key: str, config: ModelConfig) -> BaseChatModel:
    """Factory: produce a typed ChatModel from config. No fuzzy dispatch.

    Supports:
    - anthropic: native Anthropic API
    - deepseek: Anthropic-compatible protocol via custom base_url
    - openai: native OpenAI API
    """
    match config.provider:
        case "deepseek":
            return ChatAnthropic(
                api_key=api_key,
                base_url=config.base_url or "https://api.deepseek.com/anthropic",
                model=config.model,
                temperature=config.temperature,
                max_tokens=config.max_tokens,
            )
        case "anthropic":
            return ChatAnthropic(
                api_key=api_key,
                model=config.model,
                temperature=config.temperature,
                max_tokens=config.max_tokens,
            )
        case "openai":
            return ChatOpenAI(
                api_key=api_key,
                model=config.model,
                temperature=config.temperature,
                max_tokens=config.max_tokens,
            )
        case _:
            raise ValueError(f"Unknown provider: {config.provider}")
