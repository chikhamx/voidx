"""JSON-RPC 2.0 envelope models and parsing rules.

Parsing is driven by field existence, not a discriminator tag:
- id + method                      → Request
- method + no id                   → Notification
- id + result                      → Result
- id + error                       → Error
- id + no method/result/error      → invalid (-32600)
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

PROTOCOL_VERSION = "2.0"

# JSON-RPC standard error codes
ERR_PARSE_ERROR = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL_ERROR = -32603
# voidx-specific codes
ERR_THREAD_NOT_FOUND = -32000
ERR_TURN_IN_PROGRESS = -32001
ERR_TERMINAL_NOT_FOUND = -32002


class ErrorPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: int
    message: str
    data: dict[str, Any] | None = None


class JsonRpcRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    jsonrpc: str = PROTOCOL_VERSION
    id: int | str
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class JsonRpcNotification(BaseModel):
    model_config = ConfigDict(frozen=True)

    jsonrpc: str = PROTOCOL_VERSION
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class JsonRpcResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    jsonrpc: str = PROTOCOL_VERSION
    id: int | str
    result: Any


class JsonRpcError(BaseModel):
    model_config = ConfigDict(frozen=True)

    jsonrpc: str = PROTOCOL_VERSION
    id: int | str | None
    error: ErrorPayload


class ParseError(Exception):
    """Raised when a message is not a valid JSON-RPC 2.0 message."""

    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


def parse_jsonrpc_message(raw: dict[str, Any]) -> JsonRpcRequest | JsonRpcNotification | JsonRpcResult | JsonRpcError:
    """Parse a raw dict into one of the four JSON-RPC message types.

    Raises ParseError with the appropriate JSON-RPC error code if the message
    is not a valid JSON-RPC 2.0 message.
    """
    if not isinstance(raw, dict):
        raise ParseError(ERR_INVALID_REQUEST, "invalid request: not an object")

    version = raw.get("jsonrpc")
    if version != PROTOCOL_VERSION:
        raise ParseError(ERR_INVALID_REQUEST, "invalid request: missing or wrong jsonrpc version")

    has_id = "id" in raw
    has_method = "method" in raw
    has_result = "result" in raw
    has_error = "error" in raw

    # A message cannot carry both method and result/error.
    if has_method and (has_result or has_error):
        raise ParseError(ERR_INVALID_REQUEST, "invalid request: method with result/error")

    try:
        if has_method and has_id:
            return JsonRpcRequest.model_validate(raw)
        if has_method and not has_id:
            return JsonRpcNotification.model_validate(raw)
        if has_id and has_result:
            return JsonRpcResult.model_validate(raw)
        if has_id and has_error:
            return JsonRpcError.model_validate(raw)
    except ValidationError as exc:
        raise ParseError(ERR_INVALID_REQUEST, f"invalid request: {exc}") from exc

    # id present but no method/result/error → invalid
    raise ParseError(ERR_INVALID_REQUEST, "invalid request: id without method/result/error")
