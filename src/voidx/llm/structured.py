"""Shared compatibility helpers for small structured LLM requests."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Sequence
from typing import Any, Literal

from langchain_core.messages import BaseMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel

StructuredOutputMethod = Literal["auto", "json_mode", "function_calling"]


def _accepts_keyword(callable_obj: Any, keyword: str) -> bool:
    try:
        parameters = inspect.signature(callable_obj).parameters.values()
    except (TypeError, ValueError):
        return True
    return any(
        parameter.name == keyword or parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def resolve_structured_output_method(model: Any, method: StructuredOutputMethod = "auto") -> str:
    if method != "auto":
        return method
    selected = getattr(model, "resolver_structured_output_method", None)
    return selected if selected in {"json_mode", "function_calling"} else "function_calling"


def _function_calling_schema(schema: type[BaseModel]) -> Any:
    return convert_to_openai_tool(schema)


def _coerce_response_schema(response: Any, schema: type[BaseModel], *, converted_schema: bool) -> Any:
    if not converted_schema:
        return response
    if isinstance(response, schema):
        return response
    if isinstance(response, dict):
        if "parsed" in response:
            parsed = response.get("parsed")
            if parsed is None or isinstance(parsed, schema):
                return response
            if isinstance(parsed, dict):
                return {**response, "parsed": schema.model_validate(parsed)}
            return response
        return schema.model_validate(response)
    return response


async def ainvoke_structured(
    *,
    model: Any,
    schema: type[BaseModel],
    messages: Sequence[BaseMessage] | Sequence[Any],
    method: StructuredOutputMethod = "auto",
    include_raw: bool = False,
    timeout: float | None = None,
) -> Any:
    """Invoke a Pydantic structured-output request with provider compatibility."""
    structured = getattr(model, "with_structured_output", None)
    if not callable(structured):
        raise RuntimeError("model does not support structured output")

    kwargs: dict[str, Any] = {}
    if _accepts_keyword(structured, "method"):
        kwargs["method"] = resolve_structured_output_method(model, method)
    if include_raw and _accepts_keyword(structured, "include_raw"):
        kwargs["include_raw"] = True
    selected_method = kwargs.get("method")
    converted_schema = selected_method == "function_calling"
    call_schema = _function_calling_schema(schema) if converted_schema else schema
    runnable = structured(call_schema, **kwargs)
    invocation = runnable.ainvoke(messages)
    if timeout is not None:
        response = await asyncio.wait_for(invocation, timeout=timeout)
    else:
        response = await invocation
    return _coerce_response_schema(response, schema, converted_schema=converted_schema)
