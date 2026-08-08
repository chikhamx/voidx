"""LLM provider application-level capability rules."""

from typing import Any


def get_resolver_structured_output_method(model: Any) -> str | None:
    method = getattr(model, "resolver_structured_output_method", None)
    return method if method in {"json_mode", "function_calling"} else None
