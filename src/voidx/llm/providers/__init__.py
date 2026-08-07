"""Provider registry primitives.

Built-in providers are loaded explicitly by :mod:`voidx.llm.providers.registry`;
importing this package does not import provider implementations.
"""

from voidx.llm.providers.base import (
    PROTOCOL_DEEPSEEK,
    ProviderSpec,
    all_specs,
    get,
    register,
)
from voidx.llm.providers.registry import load_builtins

load_builtins()

__all__ = [
    "PROTOCOL_DEEPSEEK",
    "ProviderSpec",
    "all_specs",
    "get",
    "register",
]
