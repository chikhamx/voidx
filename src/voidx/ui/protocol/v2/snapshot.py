"""Multi-session snapshot models for protocol v2.

WorkspaceSnapshot is pushed on connect and on refresh. It contains ThreadInfo
for all threads but only the full transcript snapshot for the active thread.
Non-active threads are fetched on demand via session.switch.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from voidx.ui.protocol.transcript import TranscriptNode
from voidx.ui.protocol.v2.threads import ThreadInfo


class ThreadSnapshot(BaseModel):
    """Transcript snapshot for a single thread."""

    model_config = ConfigDict(frozen=True)

    thread_id: str
    revision: int = 0
    nodes: list[TranscriptNode] = Field(default_factory=list)


class WorkspaceSnapshot(BaseModel):
    """Full workspace state pushed on connect / refresh.

    Only the active thread carries a complete transcript snapshot; other
    threads are listed as ThreadInfo metadata only.
    """

    model_config = ConfigDict(frozen=True)

    threads: list[ThreadInfo] = Field(default_factory=list)
    active_thread_id: str = ""
    active_snapshot: ThreadSnapshot | None = None
    provider: str = ""
    model: str = ""
    workspace: str = ""
    profile_configured: bool | None = None
    permission_mode: str = ""
    ai_approval_count: int = 0
    runtime: dict[str, object] = Field(default_factory=dict)
    workspace_write_lock: dict[str, object] = Field(default_factory=dict)
