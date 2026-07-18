"""Built-in LLM providers — importing this package registers all specs.

Each provider module calls ``base.register`` at import time.  Keep the
import list exhaustive; ``voidx.llm.provider`` and ``voidx.llm.catalog``
resolve everything through the registry.
"""

from voidx.llm.providers import (
    anthropic,
    deepseek,
    doubao,
    gemini,
    kimi,
    longcat,
    mimo,
    minimax,
    openai,
    openrouter,
    qwen,
    typex,
    xunfei,
    zhipu,
)
from voidx.llm.providers.base import (
    PROTOCOL_DEEPSEEK,
    ProviderSpec,
    all_specs,
    get,
    register,
)

__all__ = [
    "PROTOCOL_DEEPSEEK",
    "ProviderSpec",
    "all_specs",
    "get",
    "register",
    "anthropic",
    "deepseek",
    "doubao",
    "gemini",
    "kimi",
    "longcat",
    "mimo",
    "minimax",
    "openai",
    "openrouter",
    "qwen",
    "typex",
    "xunfei",
    "zhipu",
]
