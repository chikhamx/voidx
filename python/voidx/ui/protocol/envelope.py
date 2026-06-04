"""Wire envelopes for UI frontend protocol messages."""

from __future__ import annotations

import time
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from voidx.ui.output.events.schema import UiEvent
from voidx.ui.protocol.commands import UiCommand
from voidx.ui.protocol.requests import UiRequest, UiResponse
from voidx.ui.protocol.transcript import TranscriptSnapshot

PROTOCOL_VERSION = 1


class UiHello(BaseModel):
    model_config = ConfigDict(frozen=True)

    client: str
    last_seq: int | None = None


class ProtocolEnvelopeBase(BaseModel):
    model_config = ConfigDict(frozen=True)

    v: int = PROTOCOL_VERSION
    seq: int = 0
    ts: float = Field(default_factory=time.time)


class UiHelloEnvelope(ProtocolEnvelopeBase):
    type: Literal["hello"] = "hello"
    payload: UiHello


class UiSnapshotEnvelope(ProtocolEnvelopeBase):
    type: Literal["snapshot"] = "snapshot"
    payload: TranscriptSnapshot


class UiEventEnvelope(ProtocolEnvelopeBase):
    type: Literal["event"] = "event"
    payload: UiEvent


class UiRequestEnvelope(ProtocolEnvelopeBase):
    type: Literal["request"] = "request"
    payload: UiRequest


class UiResponseEnvelope(ProtocolEnvelopeBase):
    type: Literal["response"] = "response"
    payload: UiResponse


class UiCommandEnvelope(ProtocolEnvelopeBase):
    type: Literal["command"] = "command"
    payload: UiCommand


ProtocolEnvelope: TypeAlias = Annotated[
    UiHelloEnvelope
    | UiSnapshotEnvelope
    | UiEventEnvelope
    | UiRequestEnvelope
    | UiResponseEnvelope
    | UiCommandEnvelope,
    Field(discriminator="type"),
]
_ENVELOPE_ADAPTER: TypeAdapter[ProtocolEnvelope] = TypeAdapter(ProtocolEnvelope)


def parse_protocol_envelope(value: object) -> ProtocolEnvelope:
    return _ENVELOPE_ADAPTER.validate_python(value)
