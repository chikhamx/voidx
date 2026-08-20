"""Method registry and dispatch for protocol v2 JSON-RPC.

Handlers are async callables taking a params dict and returning a result dict.
Sync callables are also accepted and auto-awaited via inspect.isawaitable.
"""

from __future__ import annotations

import inspect

from voidx.observability import log_internal_error
from collections.abc import Awaitable, Callable
from typing import Any

from voidx.presentation.protocol.v2.envelope import (
    ERR_INTERNAL_ERROR,
    ERR_INVALID_PARAMS,
    ERR_METHOD_NOT_FOUND,
    ErrorPayload,
    JsonRpcError,
    JsonRpcRequest,
    JsonRpcResult,
)

MethodHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]] | dict[str, Any]]


class MethodParamsError(Exception):
    """Raised by a handler when params are invalid or a business rule rejects them."""

    def __init__(
        self,
        message: str,
        *,
        code: int = ERR_INVALID_PARAMS,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.data = data
        super().__init__(message)


class MethodDispatch:
    """Registry of JSON-RPC method handlers."""

    def __init__(self) -> None:
        self._handlers: dict[str, MethodHandler] = {}

    def register(self, method: str, handler: MethodHandler) -> None:
        self._handlers[method] = handler

    def registered_methods(self) -> list[str]:
        return sorted(self._handlers.keys())

    async def dispatch(self, request: JsonRpcRequest) -> JsonRpcResult | JsonRpcError:
        handler = self._handlers.get(request.method)
        if handler is None:
            return JsonRpcError(
                id=request.id,
                error=ErrorPayload(
                    code=ERR_METHOD_NOT_FOUND,
                    message=f"method not found: {request.method}",
                ),
            )
        try:
            result = handler(request.params)
            if inspect.isawaitable(result):
                result = await result
        except MethodParamsError as exc:
            return JsonRpcError(
                id=request.id,
                error=ErrorPayload(
                    code=exc.code,
                    message=str(exc),
                    data=exc.data,
                ),
            )
        except Exception as exc:  # noqa: BLE001 — log detail, return a stable safe error
            log_internal_error(exc, context=f"gateway_method:{request.method}")
            return JsonRpcError(
                id=request.id,
                error=ErrorPayload(
                    code=ERR_INTERNAL_ERROR,
                    message="internal error",
                ),
            )
        return JsonRpcResult(id=request.id, result=result)
