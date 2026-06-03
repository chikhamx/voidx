"""JSON Schema export helpers for frontend contract generation."""

from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter

from voidx.ui.protocol.commands import UiCommand
from voidx.ui.protocol.envelope import ProtocolEnvelope
from voidx.ui.protocol.requests import UiRequest
from voidx.ui.protocol.transcript import TranscriptSnapshot


def export_protocol_schema() -> dict[str, Any]:
    envelope_schema = TypeAdapter(ProtocolEnvelope).json_schema(
        ref_template="#/$defs/{model}"
    )
    defs = dict(envelope_schema.pop("$defs", {}))
    defs["ProtocolEnvelope"] = envelope_schema
    defs.update(
        TypeAdapter(TranscriptSnapshot | UiRequest | UiCommand).json_schema(
            ref_template="#/$defs/{model}"
        ).get("$defs", {})
    )
    return {
        "title": "VoidxUiProtocol",
        "type": "object",
        "$defs": defs,
    }
