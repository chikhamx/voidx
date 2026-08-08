"""Ports for the goal resolver's external LLM capabilities."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from langchain_core.messages import BaseMessage
from pydantic import BaseModel

StructuredOutputMethod = Literal["auto", "json_mode", "function_calling"]
StructuredInvoker = Callable[..., Awaitable[Any]]
StructuredMethodResolver = Callable[[Any], str]
TokenEstimator = Callable[[Any, str], int]
TokenUsageExtractor = Callable[[object], "ResolverTokenUsage"]


@dataclass(frozen=True)
class ResolverTokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_tokens_reported: bool = False


class UsageRecorder(Protocol):
    def record_call(
        self,
        usage: Any,
        *,
        fallback_input_tokens: int = 0,
        fallback_output_tokens: int = 0,
        messages: list | None = None,
        model: str = "",
        cache_key: str = "",
    ) -> None: ...


def _accepts_keyword(callable_obj: Any, keyword: str) -> bool:
    import inspect

    try:
        parameters = inspect.signature(callable_obj).parameters.values()
    except (TypeError, ValueError):
        return True
    return any(
        parameter.name == keyword or parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


async def invoke_structured_default(
    *,
    model: Any,
    schema: type[BaseModel],
    messages: Sequence[BaseMessage] | Sequence[Any],
    method: StructuredOutputMethod = "auto",
    include_raw: bool = False,
    timeout: float | None = None,
) -> Any:
    """Provider-neutral structured invocation fallback for direct application use."""
    structured = getattr(model, "with_structured_output", None)
    if not callable(structured):
        raise RuntimeError("model does not support structured output")
    kwargs: dict[str, Any] = {}
    if _accepts_keyword(structured, "method"):
        kwargs["method"] = resolve_structured_method_default(model) if method == "auto" else method
    if include_raw and _accepts_keyword(structured, "include_raw"):
        kwargs["include_raw"] = True
    runnable = structured(schema, **kwargs)
    invocation = runnable.ainvoke(messages)
    if timeout is None:
        return await invocation
    import asyncio

    return await asyncio.wait_for(invocation, timeout=timeout)


def resolve_structured_method_default(model: Any) -> str:
    selected = getattr(model, "resolver_structured_output_method", None)
    return selected if selected in {"json_mode", "function_calling"} else "function_calling"


def no_token_estimate(_value: Any, _model: str) -> int:
    return 0


def no_token_usage(value: object) -> ResolverTokenUsage:
    metadata = getattr(value, "usage_metadata", None)
    if not isinstance(metadata, dict):
        return ResolverTokenUsage()
    input_tokens = int(metadata.get("input_tokens") or 0)
    output_tokens = int(metadata.get("output_tokens") or 0)
    return ResolverTokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=int(metadata.get("total_tokens") or input_tokens + output_tokens),
    )
