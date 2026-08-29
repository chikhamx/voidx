"""Incremental v2 gateway protocol models.

The snapshot remains the recovery contract. These models only describe
metadata patches and safe append-only stream updates negotiated per client.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from voidx.presentation.protocol.v2.threads import ThreadInfo


CAPABILITY_STREAM_APPEND = "stream_append_v1"
CAPABILITY_WORKSPACE_PATCH = "workspace_patch_v1"
CAPABILITY_TRANSCRIPT_WINDOW = "transcript_window_v1"
SUPPORTED_INCREMENTAL_CAPABILITIES = frozenset(
    {
        CAPABILITY_STREAM_APPEND,
        CAPABILITY_WORKSPACE_PATCH,
        CAPABILITY_TRANSCRIPT_WINDOW,
    }
)


class ClientCapabilities(BaseModel):
    """Capabilities announced by a UI client after the socket opens."""

    model_config = ConfigDict(frozen=True)

    protocol: str = "voidx.ui.v2"
    capabilities: list[str] = Field(default_factory=list)


class GatewayCapabilities(BaseModel):
    """Capabilities supported by this Gateway instance."""

    model_config = ConfigDict(frozen=True)

    protocol: str = "voidx.ui.v2"
    capabilities: list[str] = Field(
        default_factory=lambda: sorted(SUPPORTED_INCREMENTAL_CAPABILITIES)
    )
    revision: int = 0


class WorkspacePatch(BaseModel):
    """Metadata-only workspace update.

    It deliberately has no transcript field: a patch cannot implicitly delete
    or replace canonical transcript items. A revision gap requires a snapshot.
    """

    model_config = ConfigDict(frozen=True)

    revision: int
    active_thread_id: str = ""
    threads: list[ThreadInfo] = Field(default_factory=list)
    provider: str = ""
    model: str = ""
    workspace: str = ""
    profile_configured: bool | None = None
    permission_mode: str = ""
    ai_approval_count: int = 0
    runtime: dict[str, object] = Field(default_factory=dict)
    workspace_write_lock: dict[str, object] = Field(default_factory=dict)


class StreamAppendDelta(BaseModel):
    """A contiguous append to one assistant stream."""

    model_config = ConfigDict(frozen=True)

    item_id: str
    turn_id: str
    thread_id: str
    stream_id: str
    base_revision: int
    revision: int
    text: str
    phase: str = "text"
    workspace_revision: int = 0


__all__ = [
    "CAPABILITY_STREAM_APPEND",
    "CAPABILITY_TRANSCRIPT_WINDOW",
    "CAPABILITY_WORKSPACE_PATCH",
    "ClientCapabilities",
    "GatewayCapabilities",
    "SUPPORTED_INCREMENTAL_CAPABILITIES",
    "StreamAppendDelta",
    "WorkspacePatch",
]
