"""JSON Schema export helpers for frontend contract generation."""

from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter

from voidx.presentation.protocol.commands import UiCommand
from voidx.presentation.protocol.requests import UiRequest
from voidx.presentation.protocol.transcript import TranscriptSnapshot
from voidx.presentation.protocol.v2.agent_profiles import (
    AgentCatalogDto,
    AgentCatalogEdgeDto,
    AgentCatalogIntegrationDto,
    AgentCatalogNodeDto,
    AgentCatalogToolDto,
    AgentProfileDetailDto,
    AgentProfileDiagnosticDto,
    AgentProfileInfoDto,
    AgentProfileListDto,
    AgentProfileSaveDto,
    AgentProfileSnapshotDto,
    AgentProfileValidationDto,
)
from voidx.presentation.protocol.v2.envelope import (
    ErrorPayload,
    JsonRpcError,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResult,
)
from voidx.presentation.protocol.v2.incremental import (
    ClientCapabilities,
    GatewayCapabilities,
    StreamAppendDelta,
    WorkspacePatch,
)
from voidx.presentation.protocol.v2.snapshot import ThreadSnapshot, WorkspaceSnapshot
from voidx.presentation.protocol.v2.threads import Item, ThreadInfo, TurnInfo


def export_protocol_schema() -> dict[str, Any]:
    schema = TypeAdapter(
        AgentCatalogDto
        | AgentCatalogEdgeDto
        | AgentCatalogIntegrationDto
        | AgentCatalogNodeDto
        | AgentCatalogToolDto
        | AgentProfileDetailDto
        | AgentProfileDiagnosticDto
        | AgentProfileInfoDto
        | AgentProfileListDto
        | AgentProfileSaveDto
        | AgentProfileSnapshotDto
        | AgentProfileValidationDto
        | JsonRpcRequest
        | JsonRpcNotification
        | JsonRpcResult
        | JsonRpcError
        | ErrorPayload
        | ClientCapabilities
        | GatewayCapabilities
        | WorkspacePatch
        | StreamAppendDelta
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
