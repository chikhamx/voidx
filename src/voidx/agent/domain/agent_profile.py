"""Composable agent profile domain contracts.

A profile file is never consumed directly at runtime. ``AgentRegistry.resolve``
produces an immutable ``ResolvedAgentProfile`` snapshot once per session create
or explicit switch; every turn, goal attempt, loop iteration, and subagent
consumes that snapshot only.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from voidx.agent.domain.automation.workflow_schema import WorkflowDAG
from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.domain.run_config import RunConfig

ProfileSource = Literal["bundled", "global", "project"]
HitlMode = Literal["interactive", "autonomous"]

PROFILE_NAME_RE = re.compile(r"^(?=.{1,64}$)[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def normalize_profile_name(name: str) -> str:
    return name.strip().lower()


def canonical_payload_json(payload: Any) -> str:
    """Canonical JSON for hashing: sorted keys, compact, UTF-8."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash_of(payload: Any) -> str:
    """SHA-256 over the canonical JSON form of a payload."""
    return hashlib.sha256(canonical_payload_json(payload).encode("utf-8")).hexdigest()


class ProfileDiagnostic(BaseModel):
    """Stable loader/resolver diagnostic surfaced to configuration clients."""

    model_config = ConfigDict(frozen=True)

    path: str
    code: str
    message: str
    severity: Literal["error", "warning"] = "error"


class ResourcePolicy(BaseModel):
    """Profile resource layer: only ever narrows preset/phase defaults.

    ``None`` for skills/mcp_servers means "inherit per hitl_mode defaults"
    (interactive inherits discovered/enabled sets; autonomous treats them as
    empty). An empty tuple is an explicit opt-out.
    """

    model_config = ConfigDict(frozen=True)

    hitl_mode: HitlMode = "interactive"
    tools_allow: frozenset[str] | None = None
    tools_block: frozenset[str] = frozenset()
    skills: tuple[str, ...] | None = None
    mcp_servers: tuple[str, ...] | None = None


class AgentProfileSnapshot(BaseModel):
    """Content-addressed, source-traceable snapshot of one resolved profile."""

    model_config = ConfigDict(frozen=True)

    profile_id: str
    revision: int = Field(ge=1)
    source: ProfileSource
    content_hash: str
    snapshot_hash: str
    canonical_payload: dict[str, Any]

    @field_validator("profile_id", "content_hash", "snapshot_hash")
    @classmethod
    def _require_value(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value


class WorkflowRuntimeContext(BaseModel):
    """Workflow DAG pinned to a profile snapshot; never a global mutable service."""

    model_config = ConfigDict(frozen=True)

    dag: WorkflowDAG
    dag_revision: int = Field(ge=1)
    dag_hash: str
    source: ProfileSource

    @field_validator("dag_hash")
    @classmethod
    def _require_hash(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value


class ResolvedAgentProfile(BaseModel):
    """Immutable four-layer composition consumed by every runtime path."""

    model_config = ConfigDict(frozen=True)

    snapshot: AgentProfileSnapshot
    runtime_profile: RuntimeProfile
    workflow_context: WorkflowRuntimeContext | None = None
    run_config: RunConfig
    resource_policy: ResourcePolicy


class AgentProfileInfo(BaseModel):
    """Discovery listing metadata for one profile (RPC-safe; no prompts/paths)."""

    model_config = ConfigDict(frozen=True)

    name: str
    display_name: str = ""
    revision: int = 0
    content_hash: str = ""
    source: ProfileSource = "project"
    run_mode: str = "single"
    hitl_mode: HitlMode = "interactive"
    available: bool = True
    diagnostics: tuple[ProfileDiagnostic, ...] = ()
