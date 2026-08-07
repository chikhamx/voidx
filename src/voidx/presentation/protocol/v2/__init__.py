"""Protocol v2 JSON-RPC DTOs and helpers."""

from voidx.presentation.protocol.v2.envelope import (
    ERR_INTERNAL_ERROR,
    ERR_INVALID_PARAMS,
    ERR_INVALID_REQUEST,
    ERR_METHOD_NOT_FOUND,
    ERR_PARSE_ERROR,
    ERR_TERMINAL_NOT_FOUND,
    ERR_THREAD_NOT_FOUND,
    ERR_TURN_IN_PROGRESS,
    ErrorPayload,
    JsonRpcError,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResult,
    PROTOCOL_VERSION,
    ParseError,
    parse_jsonrpc_message,
)
from voidx.presentation.protocol.v2.snapshot import ThreadSnapshot, WorkspaceSnapshot
from voidx.presentation.protocol.v2.threads import Item, ThreadInfo, TurnInfo

__all__ = [
    "ERR_INTERNAL_ERROR",
    "ERR_INVALID_PARAMS",
    "ERR_INVALID_REQUEST",
    "ERR_METHOD_NOT_FOUND",
    "ERR_PARSE_ERROR",
    "ERR_TERMINAL_NOT_FOUND",
    "ERR_THREAD_NOT_FOUND",
    "ERR_TURN_IN_PROGRESS",
    "ErrorPayload",
    "Item",
    "JsonRpcError",
    "JsonRpcNotification",
    "JsonRpcRequest",
    "JsonRpcResult",
    "PROTOCOL_VERSION",
    "ParseError",
    "ThreadInfo",
    "ThreadSnapshot",
    "TurnInfo",
    "WorkspaceSnapshot",
    "parse_jsonrpc_message",
]
