"""Public JSON-RPC DTOs for agent profile configuration."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class AgentProfileDiagnosticDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    code: str
    message: str
    severity: Literal["error", "warning"] = "error"


class AgentProfileInfoDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    display_name: str
    revision: int
    content_hash: str
    source: Literal["bundled", "global", "project"]
    run_mode: str
    hitl_mode: Literal["interactive", "autonomous"]
    availability: Literal["available", "unavailable"]
    diagnostics: tuple[AgentProfileDiagnosticDto, ...] = ()


class AgentProfileSnapshotDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    profile_id: str
    revision: int
    source: Literal["bundled", "global", "project"]
    content_hash: str
    snapshot_hash: str


class AgentProfileValidationDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    valid: bool
    diagnostics: tuple[AgentProfileDiagnosticDto, ...] = ()
    snapshot: AgentProfileSnapshotDto | None = None


class AgentProfileSaveDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    snapshot: AgentProfileSnapshotDto
    diagnostics: tuple[AgentProfileDiagnosticDto, ...] = ()


class AgentProfileDetailDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    profile: AgentProfileInfoDto
    yaml: str
    read_only: bool


class AgentProfileListDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    profiles: tuple[AgentProfileInfoDto, ...] = ()


class AgentCatalogToolDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    description: str


class AgentCatalogIntegrationDto(BaseModel):
    """Checkbox-list entry for skills / MCP servers (UI consumes name+description)."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str


class AgentCatalogNodeDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    description: str


class AgentCatalogEdgeDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: str
    target: str
    condition: str
    label: str = ""


class AgentCatalogDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    tools: tuple[AgentCatalogToolDto, ...] = ()
    skills: tuple[AgentCatalogIntegrationDto, ...] = ()
    mcp_servers: tuple[AgentCatalogIntegrationDto, ...] = ()
    builtin_nodes: tuple[AgentCatalogNodeDto, ...] = ()
    default_edges: tuple[AgentCatalogEdgeDto, ...] = ()
