"""JSON Schema export helpers for frontend contract generation."""

from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter

from voidx.ui.protocol.commands import UiCommand
from voidx.ui.protocol.requests import UiRequest
from voidx.ui.protocol.transcript import TranscriptSnapshot
from voidx.ui.protocol.v2.envelope import (
    ErrorPayload,
    JsonRpcError,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResult,
)
from voidx.ui.protocol.v2.snapshot import ThreadSnapshot, WorkspaceSnapshot
from voidx.ui.protocol.v2.threads import Item, ThreadInfo, TurnInfo


def export_protocol_schema() -> dict[str, Any]:
    schema = TypeAdapter(
        JsonRpcRequest
        | JsonRpcNotification
        | JsonRpcResult
        | JsonRpcError
        | ErrorPayload
        | WorkspaceSnapshot
        | ThreadSnapshot
        | ThreadInfo
        | TurnInfo
        | Item
        | TranscriptSnapshot
        | UiRequest
        | UiCommand
    ).json_schema(ref_template="#/$defs/{model}")
    return {
        "title": "VoidxUiProtocol",
        **schema,
    }
